import os
import logging
import torch
from transformers import AutoTokenizer, TextStreamer
from tpi_llm import TPILlamaForCausalLM
from tpi_llm.split import split_pretrained_model
from tpi_llm.modeling_utils import load_model_config


logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    datefmt="%m/%d/%Y %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

MODEL_CLASSES = {
    "llama": (TPILlamaForCausalLM, AutoTokenizer),
}
MAX_LENGTH = int(10000)  # Hardcoded max length to avoid infinite loop


def adjust_length_to_model(length, max_sequence_length):
    if length < 0 and max_sequence_length > 0:
        length = max_sequence_length
    elif 0 < max_sequence_length < length:
        length = max_sequence_length  # No generation bigger than model size
    elif length < 0:
        length = MAX_LENGTH  # avoid infinite loop
    return length


def main(my_rank, world_size, args):
    torch.manual_seed(args.seed)
    if args.use_gpu:
        torch.cuda.manual_seed_all(args.seed)
    model_config = load_model_config(args.model_path)
    max_seq_length = model_config.get("max_position_embeddings", 0)
    args.length = adjust_length_to_model(args.length, max_sequence_length=max_seq_length)
    args.device = "cuda" if args.use_gpu else "cpu"
    args.rank = my_rank
    assert args.memory_window >= 2, "Memory window should be larger than 10."
    logger.info(f"My rank is {my_rank}, running on device {args.device}.")

    try:
        args.model_type = args.model_type.lower()
        model_class, tokenizer_class = MODEL_CLASSES[args.model_type]
    except KeyError:
        raise KeyError(f"Unsupported model type: {args.model_type}")

    # todo: automatically determine ratio
    args.ratio = [1./args.world_size] * args.world_size

    # split pretrained model files on node with rank 0.
    if my_rank == 0 and args.split_bin:
        if not os.path.exists(args.model_path):
            raise Exception(f"Model path {args.model_path} does not exist, "
                            f"please download the pretrained model parameters first.")

        split_file_path = os.path.join(args.model_path, args.save_dir)
        split_pretrained_model(
            model_path=args.model_path,
            world_size=world_size,
            ratio=args.ratio,
            save_dir=args.save_dir
        )
        logger.info(f"All weights are splitted and saved to {split_file_path}.")
    else:
        os.makedirs(args.model_path, exist_ok=True)
        # todo: each node download sliced weight files from the master node.

    # load tokenizer and encode user prompt
    tokenizer, streamer = None, None
    input_ids = ""
    if my_rank == 0:
        tokenizer = tokenizer_class.from_pretrained(args.model_path)
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token

        streamer = TextStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True)

        prompt_text = args.prompt if args.prompt else input("User prompt >>> ")
        input_ids = tokenizer.encode(
            args.prefix + prompt_text,
            add_special_tokens=False,
            return_tensors="pt"
        ).to(args.device)

    # load model and run tensor-parallelism inference
    model = model_class.from_pretrained(args.model_path, rank=my_rank, args=args)

    # run generate with streaming output
    model.generate(
        input_ids=input_ids,
        max_length=args.length + len(input_ids[0]) if my_rank == 0 else args.length,
        temperature=args.temperature,
        top_k=args.k,
        top_p=args.p,
        do_sample=True,
        streamer=streamer,
    )

    # print recorded memory usage
    # time_str, mem_str = model.mem_manager.memory_history
    # logger.info(f"RANK {my_rank}:\nTimestamp: {time_str} \nMemory usage: {mem_str}")
