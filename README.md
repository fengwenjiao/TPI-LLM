# TPI-LLM: High-Performance Tensor Parallelism Inference System for Edge LLM Services.
TPI-LLM (Tensor Parallelism Inference for Large Language Models) is a LLM service system designed to bring LLM 
functions to resource-constrained edge networks. While cloud LLM services have achieved great success, privacy 
concerns arise and users do not want their conversations uploaded to the cloud as these conversations could 
involve sensitive personal information.

Our TPI-LLM system addresses the privacy issue by enabling LLM inference on edge devices with limited resources. 
The system leverages multiple edge devices to perform inference through tensor parallelism, combined with 
sophisticated memory window scheduling techniques to minimize memory usage. Currently, TPI-LLM can run the 
full-precision Llama-2-3B model on a single Mac with only 8GB of RAM, while maintaining a stable memory footprint 
below 0.7 GB. In the future, we will support larger models, such as Llama-3.1-70B and Llama-3.1-405B, across multiple edge 
devices, and introduce acceleration techniques to ensure efficient inference.

# Updates
* 2024/08/20: Add support for multi-host tensor parallelism.
* 2024/08/22: Add support for Llama 2, Llama 3 and Llama 3.1.
* 2024/08/26: Implement a file server to synchronize sliced model files to other nodes.

# Installation
1. Clone the repository:
```commandline
> git clone https://github.com/Lizonghang/TPI-LLM
> cd TPI-LLM
```

2. Add `PYTHONPATH` to `.bashrc`:
```commandline
> vim ~/.bashrc

# Set PYTHONPATH to the TPI-LLM/src folder
export PYTHONPATH=<PATH-TO-TPI-LLM>/src
```

3. Create a new conda environment and install dependencies:
```commandline
> conda create -n tpi-llm python=3.9
> conda activate tpi-llm
(tpi-llm) > pip install -r requirements.txt
```

# How to Use?

**1. Download Pretrained Model Weights**

To get started, you’ll need to download the pretrained model weights from **Hugging Face**:

- **Llama 2 series**, for example, [Meta/Llama-2-7b-hf](https://huggingface.co/meta-llama/Llama-2-7b-hf)
- **Llama 3 series**, for example, [Meta/Llama-3-8b](https://huggingface.co/meta-llama/Meta-Llama-3-8B/tree/main)
- **Llama 3.1 series**, for example, [Meta/Llama-3.1-8b-Instruct](https://huggingface.co/meta-llama/Meta-Llama-3.1-8B-Instruct)

After downloading, save the model files in a directory of your choice, which we’ll refer to as `<PATH-TO-MODEL-FILES>`.

## Run on Your Laptop
Run the example script for a trial:
```commandline
> python examples/run_multiprocess.py \
    --model_type llama \
    --model_path <PATH-TO-MODEL-FILES> \
    --world_size 4 \
    --length 10 \
    --split_bin
```
This command will run 4 processes on a single machine, creating a pseudo-distributed environment that leverages 
tensor parallelism for Llama inference.

**First-Time Setup:**

If this is your first time running the script, make sure to include the <code>--split_bin</code> option. 
This will slice the pretrained model weights and save them into subdirectories corresponding to the 4 nodes:


```commandline
> ls <PATH-TO-MODEL-FILES>
|- config.json
|- model-00001-of-00004.safetensors
|- model-00002-of-00004.safetensors
|- model-00003-of-00004.safetensors
|- model-00004-of-00004.safetensors
|- model.safetensors.index.json
|- ...
|- split/
|--- node_0
|--- node_1
|--- node_2
|--- node_3
```

**Subsequent Runs:**

For subsequent runs, you can omit the <code>--split_bin</code> option, as the model weights will already be sliced 
and saved in the respective node directories.

## Run on Multiple Hosts
Assume we have four hosts 0 ~ 3. Run the following command on each of them:

```commandline
# On node 0:
> RANK=0 WORLD_SIZE=4 MASTER_ADDR=<RANK_0_IP> MASTER_PORT=29500 \
  python examples/run_multihost.py --model_type llama --model_path <PATH-TO-MODEL-FILES> --file_port 29600 --length 10 --split_bin

# On node 1:
> RANK=1 WORLD_SIZE=4 MASTER_ADDR=<RANK_0_IP> MASTER_PORT=29500 \
  python examples/run_multihost.py --model_type llama --model_path <PATH-TO-MODEL-FILES> --file_port 29600

# On node 2:
> RANK=2 WORLD_SIZE=4 MASTER_ADDR=<RANK_0_IP> MASTER_PORT=29500 \
  python examples/run_multihost.py --model_type llama --model_path <PATH-TO-MODEL-FILES> --file_port 29600
    
# On node 3:
> RANK=3 WORLD_SIZE=4 MASTER_ADDR=<RANK_0_IP> MASTER_PORT=29500 \
  python examples/run_multihost.py --model_type llama --model_path <PATH-TO-MODEL-FILES> --file_port 29600
```

You can set `<MASTER_ADDR>` and `<MASTER_PORT>` of your choice, but make sure that the master node can be accessed 
by all other nodes.

> **NOTE:** Unfortunately, Gloo depends on a specific part of Linux, the current version only supports distributed 
> between multiple Linux operating system devices. If you have needs for MacOS and Windows operating systems, 
> please modify the communication backend of PyTorch from Gloo to MPI, which may require some complicated operations.

This will start a file server on the master node 0. Nodes 1 ~ 3 will automatically download their respective 
model parameter slice files from the master node when needed. To force a re-download of the model parameter 
slice files, you can use the `--force_download` option.

## Run on Klonet
Coming soon.

## Optional Arguments
TPI-LLM provides several optional parameters that you can customize to control various aspects of the inference process. 
Below is a list of these options:

| Argument           | Default       | Type    | Description                                                            |
|--------------------|---------------|---------|------------------------------------------------------------------------|
| `--prompt`         | `""`          | `str`   | The input prompt.                                                      |
| `--length`         | `20`          | `int`   | Maximum length of the generated sequence.                              |
| `--prefix`         | `""`          | `str`   | Text added prior to input for context.                                 |
| `--use_gpu`        | `False`       | `bool`  | Whether to use GPU for inference. If false, use CPU by default.        |
| `--split_bin`      | `False`       | `bool`  | Split the pretrained model file.                                       |
| `--save_dir`       | `"split"`     | `str`   | The directory to save split model files.                               |
| `--seed`           | `42`          | `int`   | Random seed for reproducibility.                                       |
| `--master_ip`      | `"127.0.0.1"` | `str`   | IP address of the master node.                                         |
| `--master_port`    | `29500`       | `int`   | Port number of the master node.                                        |
| `--file_port`      | `29600`       | `str`   | Port number on the master node where the file server is bound.         |
| `--force_download` | `False`       | `bool`  | Force non-master nodes to re-download model parameter slice files.     |
| `--temperature`    | `1.0`         | `float` | Sampling temperature for text generation.                              |
| `--k`              | `0`           | `int`   | Number of highest probability tokens to keep for top-k sampling.       |
| `--p`              | `0.9`         | `float` | Cumulative probability for nucleus sampling (top-p).                   |
| `--memory_window`  | `2`           | `int`   | Size of the memory window used during inference. Should be at least 2. |
