#!/bin/bash

#CONFIGS=(125M.yml 6-7B.yml)
CONFIGS=(6-7B.yml)
CONT_NAME="gpt-neox-container"
#BATCHS=(1 2 4 8 16 32 64 128)
BATCHS=(32)
#GPUS=(1 2 3 4 5 6 7 8)
GPUS=(8)
TRAIN_ITERS=200
TARGET_LM_LOSS=0
TRAIN_TIME=1000

docker stop $CONT_NAME
rm logs/*
rm checkpoints/* -rf

for conf in ${CONFIGS[@]}; do
	sed -i "/\"train_iters\"/c\   \"train_iters\": \\$TRAIN_ITERS," configs/$conf
	sed -i "/\"lr_decay_iters\"/c\   \"lr_decay_iters\": \\$TRAIN_ITERS," configs/$conf
	sed -i "/\"target_lm_loss\"/c\   \"target_lm_loss\": \\$TARGET_LM_LOSS," configs/$conf
	sed -i "/\"target_time_in_sec\"/c\   \"target_time_in_sec\": \\$TRAIN_TIME," configs/$conf
	for i in ${GPUS[@]}; do
#		docker run -d -it --name $CONT_NAME --rm --gpus $i -e NVIDIA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 --shm-size=1g --ulimit memlock=-1 --mount type=bind,src=$PWD,dst=/gpt-neox swsok/gpt-neox:v3
		docker run -d -it --name $CONT_NAME --rm --gpus $i -e NVIDIA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 --shm-size=1g --ulimit memlock=-1 --mount type=bind,src=$PWD,dst=/gpt-neox swsok/gpt-neox:v4

		for b in ${BATCHS[@]}; do
			echo "$conf GPU $i microbatch $b" > logs/current_test_setting.txt
			
			sed -i "/\"train_micro_batch_size_per_gpu\"/c\   \"train_micro_batch_size_per_gpu\": \\$b," configs/$conf
			docker exec -it -w /gpt-neox $CONT_NAME ./deepy.py train.py configs/$conf configs/local_setup.yml
			mv logs/*stdout.txt swsok-results/conf-$conf-gpunum-$i-microbatch-$b-$(date '+%Y-%m-%d').txt
			rm logs/*
			rm checkpoints/* -rf
		done

		docker stop $CONT_NAME
		sleep 1
	done
done