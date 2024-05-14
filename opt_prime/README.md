# OptimusPrime: Highly-efficient 3D parallelization framework for training and inference with giant models

OptimusPrime is a 3D parallelization framework designed for the efficient training and inference of large-scale DNN models. 
It analyzes the model structure in the form of a PyTorch FX graph within a deep learning cluster environment composed of multiple GPUs and nodes. 
Based on this analysis, OptimusPrime derives optimized parallelization policies tailored to the hardware environment of the cluster, 
enabling efficient parallel training and inference.

## Supported models

OptimusPrime supports the following major models from HuggingFace:

* gpt2
* gpt2-medium 
* gpt2-large
* gpt2-xl
* EleutherAI/gpt-neo-2.7B
* EleutherAI/gpt-j-6B
* bert-base-cased

## Features

* Currently supported
  * **Pipeline parallelism**: GPipe/1F1B scheduling algorithms are supported 
* Future enhancements
  * **Data Parallelism**: will be added soon
  * **Tensor Parallelism**: planned to support for complete 3D parallelization

## Installation

To install OptimusPrime:

    # Make sure PyTorch >= 1.8.0 is installed, as well as compatible CUDA and cuDNN libraries
    git clone https://github.com/ai-computing/aicomp.git

## Running (Example: gpt2)

### Single-node environment

    cd opt_prime/examples
    torchrun --nproc_per_node=<# of GPUs per node> --nnodes=<# of nodes> --node_rank=0 --master_addr=127.0.0.1 --master_port=29500 pp_train_gpt2.py

### Multi-node environment

Run the following command for every node:

    cd opt_prime/examples
    torchrun --nproc_per_node=<# of GPUs per node> --nnodes=<# of nodes> --node_rank=<current node rank> --master_addr=<IP of rank 0> --master_port=29500 pp_train_gpt2.py

### Changing scheduling policy for pipeline parallelism

1. Open the training script file for corresponding DNN model in opt_prime/examples. (e.g. gpt2, bert)
2. Edit the line beginning with optimus_p.run in train() function.
   * optimus_p.run(data, labels): use the default scheduler (GPipe)
   * optimus_p.run(data, labels, mode="gpipe"): specify the GPipe scheduler explicitly
   * optimus_p.run(data, labels, mode="1f1b"): use the 1F1B scheduler

## License

The results of the AIcomp project are distributed under the 3-clause BSD license.