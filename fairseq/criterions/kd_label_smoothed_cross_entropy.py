# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import math
import numpy as np
from dataclasses import dataclass, field

import torch
import torch.nn.functional as F
from fairseq import metrics, utils
from fairseq.criterions import FairseqCriterion, register_criterion
from fairseq.dataclass import FairseqDataclass
from omegaconf import II


@dataclass
class KDLabelSmoothedCrossEntropyCriterionConfig(FairseqDataclass):
    label_smoothing: float = field(
        default=0.0,
        metadata={"help": "epsilon for label smoothing, 0 means no label smoothing"},
    )
    report_accuracy: bool = field(
        default=False,
        metadata={"help": "report accuracy metric"},
    )
    ignore_prefix_size: int = field(
        default=0,
        metadata={"help": "Ignore first N tokens"},
    )
    alpha: float = field(
        default=0,
        metadata={"help": "KD loss weightage, 0 means pure training without KD"}
    )
    sentence_avg: bool = II("optimization.sentence_avg")


def label_smoothed_nll_loss(lprobs, target, epsilon, ignore_index=None, reduce=True):
    if target.dim() == lprobs.dim() - 1:
        target = target.unsqueeze(-1)
    nll_loss = -lprobs.gather(dim=-1, index=target)
    smooth_loss = -lprobs.sum(dim=-1, keepdim=True)
    if ignore_index is not None:
        pad_mask = target.eq(ignore_index)
        nll_loss.masked_fill_(pad_mask, 0.0)
        smooth_loss.masked_fill_(pad_mask, 0.0)
    else:
        nll_loss = nll_loss.squeeze(-1)
        smooth_loss = smooth_loss.squeeze(-1)
    if reduce:
        nll_loss = nll_loss.sum()
        smooth_loss = smooth_loss.sum()
    eps_i = epsilon / (lprobs.size(-1) - 1)
    loss = (1.0 - epsilon - eps_i) * nll_loss + eps_i * smooth_loss
    return loss, nll_loss


@register_criterion(
    "kd_label_smoothed_cross_entropy", dataclass=KDLabelSmoothedCrossEntropyCriterionConfig
)
class KDLabelSmoothedCrossEntropyCriterion(FairseqCriterion):
    def __init__(
        self,
        task,
        sentence_avg,
        label_smoothing,
        alpha,
        ignore_prefix_size=0,
        report_accuracy=False,
    ):
        super().__init__(task)
        self.sentence_avg = sentence_avg
        self.eps = label_smoothing
        self.ignore_prefix_size = ignore_prefix_size
        self.report_accuracy = report_accuracy
        # new parameters
        self.alpha = alpha
        self.queue = torch.cuda.FloatTensor([])
        self.teacher_loss_queue = torch.cuda.FloatTensor([])
        

    def push_to_FIFO_queue(self, tensor):
        tensor = tensor.detach().view(-1)
        tensor_size = tensor.size(0)
        current_size = self.queue.size(0)
        self.queue = self.queue.view(-1)
        if tensor_size + current_size < self.task.difficult_queue_size:
            self.queue = torch.cat((self.queue, tensor))
        else:
            self.queue = torch.cat((self.queue[tensor_size: ], tensor))
    
    
    def push_to_teacher_FIFO_queue(self, tensor):
        tensor = tensor.detach().view(-1)
        tensor_size = tensor.size(0)
        current_size = self.teacher_loss_queue.size(0)
        self.teacher_loss_queue = self.teacher_loss_queue.view(-1)
        if tensor_size + current_size < self.task.difficult_queue_size:
            self.teacher_loss_queue = torch.cat((self.teacher_loss_queue, tensor))
        else:
            self.teacher_loss_queue = torch.cat((self.teacher_loss_queue[tensor_size: ], tensor))


    def forward(self, model, sample, reduce=True):
        """Compute the loss for the given sample.

        Returns a tuple with three elements:
        1) the loss
        2) the sample size, which is used as the denominator for the gradient
        3) logging outputs to display while training
        """
        net_output = model(**sample["net_input"])

        teacher_output = sample.get("teacher_output", None)

        loss, nll_loss, extra = self.compute_loss(
            model, 
            net_output, 
            sample, 
            reduce=reduce, 
            teacher_output=teacher_output, 
            distil_strategy=self.task.distil_strategy)

        sample_size = (
            sample["target"].size(0) if self.sentence_avg else sample["ntokens"]
        )

        logging_output = {
            'loss': loss.data,
            'nll_loss': nll_loss.data if nll_loss is not None else loss.data,
            'ntokens': sample['ntokens'],
            'nsentences': sample['target'].size(0),
            'sample_size': sample_size,
            'kd_loss': extra['kd_loss'].data if extra.get('kd_loss', None) is not None else 0,
            'golden_loss_student': extra['golden_loss_student'].data if extra.get('golden_loss_student', None) is not None else 0,
            'golden_loss_teacher': extra['golden_loss_teacher'].data if extra.get('golden_loss_teacher', None) is not None else 0
        }
        
        if self.report_accuracy:
            n_correct, total = self.compute_accuracy(model, net_output, sample)
            logging_output["n_correct"] = utils.item(n_correct.data)
            logging_output["total"] = utils.item(total.data)
        return loss, sample_size, logging_output


    def get_lprobs_and_target(self, model, net_output, sample):
        lprobs = model.get_normalized_probs(net_output, log_probs=True)
        target = model.get_targets(sample, net_output)
        if self.ignore_prefix_size > 0:
            # lprobs: B x T x C
            lprobs = lprobs[:, self.ignore_prefix_size :, :].contiguous()
            target = target[:, self.ignore_prefix_size :].contiguous()
        return lprobs.view(-1, lprobs.size(-1)), target.view(-1)


    def compute_loss(self, model, net_output, sample, reduce=True, teacher_output=None, distil_strategy="word_and_seq_level"):
        lprobs = model.get_normalized_probs(net_output, log_probs=True)
        lprobs = lprobs.view(-1, lprobs.size(-1))
        target = model.get_targets(sample, net_output)
        target = target.view(-1, 1)
        pad_mask = target.eq(self.padding_idx).view(-1)
        extra = {}

        # get student logits
        student_logits = net_output[0]
        student_logits = student_logits.view(-1, student_logits.size(-1))
        student_logits_T = student_logits/self.task.student_temp

        # get teacher probs
        teacher_logits = teacher_output[0]
        teacher_logits = teacher_logits.view(-1, teacher_logits.size(-1))
        teacher_probs_T = F.softmax(teacher_logits/self.task.teacher_temp, dim=-1)

        # compute teacher log-probs to get teacher loss value
        teacher_lprobs = sample.get("teacher_lprobs", None)

        # compute preliminary loss and nll_loss of student_model
        golden_loss, nll_loss = label_smoothed_nll_loss(
            lprobs, 
            target, 
            self.eps, 
            ignore_index=self.padding_idx, 
            reduce=(reduce and (distil_strategy == 'word_and_seq_level'))
        )

        if teacher_lprobs is not None:
            # compute preliminary loss and nll_loss of teacher_model
            teacher_lprobs = teacher_lprobs.view(-1, teacher_lprobs.size(-1))
            golden_loss_teacher, _ = label_smoothed_nll_loss(
                teacher_lprobs, 
                target, 
                self.eps, 
                ignore_index=self.padding_idx, 
                reduce=(reduce and (distil_strategy == 'word_and_seq_level'))
            )

        if teacher_output is None:
            # this option can be used for seq-level distillation as mentioned in Kim and Rush
            # Use the result of running beam search and taking the highest-scoring sequence with the teacher model as the training data for the student model
            loss = golden_loss
        
        elif distil_strategy == 'word_and_seq_level':
            # this option can be used for word-level distillation as mentioned in Kim and Rush
            # Student is trained on the original data and additionally trained to minimize the cross-entropy of the teacher distribution at the word-level.
            # if you use the result of beam-search of the teacher-model as the training data for the student model, then we can perform Word-KD + Seq-KD
            kd_loss = F.cross_entropy(
                student_logits_T,
                teacher_probs_T,
                reduction='none'
            )
            kd_loss = kd_loss.sum(dim=-1)
            kd_loss.masked_fill_(pad_mask, 0.)
            kd_loss = kd_loss.sum()
            extra['kd_loss'] = kd_loss
            extra['golden_loss_teacher'] = golden_loss_teacher.sum()
            extra['golden_loss_student'] = golden_loss.sum()
            loss = (1.0 - self.alpha) * golden_loss.sum() + self.alpha * kd_loss
            
        elif distil_strategy == 'batch_level':
            nll_loss = nll_loss.view(-1)
            nll_loss = nll_loss[~pad_mask]
            words_num = nll_loss.size(0)
            loss_gate = nll_loss.topk(
                math.ceil(
                    words_num * self.task.distil_rate
                ), 
                dim=0, 
                largest=True
            )[0][-1]
            KD_mask = nll_loss < loss_gate
            kd_loss = F.cross_entropy(
                student_logits_T,
                teacher_probs_T,
                reduction='none'
            )
            kd_loss = kd_loss.sum(dim=-1).view(-1)
            kd_loss = kd_loss[~pad_mask][~KD_mask]
            kd_loss = kd_loss.sum()
            nll_loss = nll_loss.sum()
            extra['kd_loss'] = kd_loss
            extra['golden_loss_teacher'] = golden_loss_teacher.sum()
            extra['golden_loss_student'] = golden_loss.sum()
            loss = (1.0 - self.alpha) * golden_loss.sum() + self.alpha * kd_loss
            
        elif distil_strategy == 'global_level':
            nll_loss = nll_loss.view(-1) 
            golden_loss = golden_loss.view(-1)
            word_rate = self.task.distil_rate
            teacher_probs_T = teacher_probs_T[~pad_mask]
            student_lprobs_T = student_lprobs_T[~pad_mask]
            nll_loss = nll_loss[~pad_mask]
            golden_loss = golden_loss[~pad_mask]
            golden_loss_teacher = golden_loss_teacher[~pad_mask]
            # get kl loss
            kd_loss = F.cross_entropy(
                student_logits_T,
                teacher_probs_T,
                reduction='none'
            )
            kd_loss = kd_loss.sum(dim=-1).view(-1) # B * T 
            # from the queue get the gate
            self.push_to_FIFO_queue(nll_loss)
            loss_gate = self.queue.topk(
                math.ceil(
                    self.queue.size(0) * word_rate
                ), 
                dim=0, 
                largest=True
            )[0][-1]
            KD_mask = nll_loss < loss_gate # B * T
            kd_loss = kd_loss.view(-1)
            kd_loss = kd_loss[~KD_mask]
            kd_loss = kd_loss.sum()
            nll_loss = nll_loss.sum()
            extra['kd_loss'] = kd_loss
            extra['golden_loss_teacher'] = golden_loss_teacher.sum()
            extra['golden_loss_student'] = golden_loss.sum()
            loss = (1.0 - self.alpha) * golden_loss.sum() + self.alpha * kd_loss
        return loss, nll_loss, extra


    def compute_accuracy(self, model, net_output, sample):
        lprobs, target = self.get_lprobs_and_target(model, net_output, sample)
        mask = target.ne(self.padding_idx)
        n_correct = torch.sum(
            lprobs.argmax(1).masked_select(mask).eq(target.masked_select(mask))
        )
        total = torch.sum(mask)
        return n_correct, total


    @staticmethod
    def reduce_metrics(logging_outputs) -> None:
        """Aggregate logging outputs from data parallel training."""
        # sum metrics
        loss = sum(log.get('loss', 0) for log in logging_outputs)
        nll_loss = sum(log.get('nll_loss', 0) for log in logging_outputs)
        ntokens = sum(log.get('ntokens', 0) for log in logging_outputs)
        sample_size = sum(log.get('sample_size', 0) for log in logging_outputs)
        # sum new metrics
        golden_loss_teacher = sum(log.get('golden_loss_teacher', 0) for log in logging_outputs)
        golden_loss_student = sum(log.get('golden_loss_student', 0) for log in logging_outputs)
        kd_loss = sum(log.get('kd_loss', 0) for log in logging_outputs)
        # log metrics
        metrics.log_scalar(
            'loss', 
            loss / sample_size / math.log(2), 
            sample_size, 
            round=3
        )
        metrics.log_scalar(
            'nll_loss', 
            nll_loss / ntokens / math.log(2), 
            ntokens, 
            round=3)
        metrics.log_scalar(
            'golden_loss_teacher', 
            golden_loss_teacher / ntokens / math.log(2), 
            ntokens, 
            round=3)
        metrics.log_scalar(
            'golden_loss_student', 
            golden_loss_student / ntokens / math.log(2), 
            ntokens, 
            round=3)
        metrics.log_scalar(
            'kd_loss', 
            kd_loss / ntokens / math.log(2), 
            ntokens, 
            round=3)
        metrics.log_derived(
            'ppl', 
            lambda meters: utils.get_perplexity(meters['nll_loss'].avg))

        total = utils.item(sum(log.get("total", 0) for log in logging_outputs))
        if total > 0:
            metrics.log_scalar("total", total)
            n_correct = utils.item(
                sum(log.get("n_correct", 0) for log in logging_outputs)
            )
            metrics.log_scalar("n_correct", n_correct)
            metrics.log_derived(
                "accuracy",
                lambda meters: round(
                    meters["n_correct"].sum * 100.0 / meters["total"].sum, 3
                )
                if meters["total"].sum > 0
                else float("nan"),
            )


    @staticmethod
    def logging_outputs_can_be_summed() -> bool:
        """
        Whether the logging outputs returned by `forward` can be summed
        across workers prior to calling `reduce_metrics`. Setting this
        to True will improves distributed training speed.
        """
        return True
