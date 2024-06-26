#!/usr/bin/env zsh

dir="$1"
cp="$dir/checkpoints/checkpoint_last.pt"

echo "dir: $dir"

declare -A tasks
tasks[cola]="/private/home/jgu/data/GLUE/CoLA-bin"
tasks[qnli]="/private/home/jgu/data/GLUE/QNLI-bin"
tasks[mrpc]="/private/home/jgu/data/GLUE/MRPC-bin"
tasks[rte]="/private/home/jgu/data/GLUE/RTE-bin"
tasks[sst_2]="/private/home/jgu/data/GLUE/SST-2-bin"

for task data_path in ${(kv)tasks}; do
    PYTHONPATH=. PREFIX="${PREFIX}" SUFFIX="" nohup python fairseq_cli/hydra_train.py -m --config-dir examples/roberta/config/finetuning \
    --config-name $task hydra/launcher=submitit_slurm +run_config=slurm_1g task.data="$data_path" hydra.launcher.name=finetune_${task}_${PREFIX} \
    checkpoint.restore_file="$cp" hydra.sweep.dir="$dir/finetune/$task" &
done
