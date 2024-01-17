import argparse
import json
import logging
import os
import random
from datetime import datetime

import datasets
import numpy as np
import torch
import torch.nn.functional as F
from data_collator import DataCollatorCustom
from peft import LoraConfig, TaskType, get_peft_model
from torch import nn
from torch.utils.data import DataLoader
from tqdm import tqdm

# from model import GPT2Config, GPT2LMModel
# import loralib as lora
from transformers import AutoModelForCausalLM, AutoTokenizer, default_data_collator, get_linear_schedule_with_warmup


# os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["CUDA_VISIBLE_DEVICES"] = "1"
# os.environ["CUDA_LAUNCH_BLOCKING"] = "1"
# os.environ["MASTER_ADDR"] = "localhost"
# os.environ["MASTER_PORT"] = "29500"
# os.environ['LOCAL_RANK'] = "0"

start_datetime = datetime.now().strftime("%Y-%m-%d_%H:%M:%S")


def main(args):
    device = args.device
    batch_size = args.batch_size

    # Data preprocessing
    tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token_id = tokenizer.eos_token_id

    train_dataset = DataCollatorCustom(filename=args.train_data_path, tokenizer=tokenizer, max_length=args.max_length)
    train_dataset = datasets.Dataset.from_dict(train_dataset.load_dataset())
    print("Input:", train_dataset["input_ids"][0])
    print("Label:", train_dataset["labels"][0])
    print("Length:", len(train_dataset["input_ids"][0]), len(train_dataset["labels"][0]))

    eval_dataset = DataCollatorCustom(filename=args.valid_data_path, tokenizer=tokenizer, max_length=args.max_length)
    eval_dataset = datasets.Dataset.from_dict(eval_dataset.load_dataset())

    train_dataloader = DataLoader(
        train_dataset,
        shuffle=True,
        collate_fn=default_data_collator,
        batch_size=batch_size,
        pin_memory=True,
    )

    eval_dataloader = DataLoader(
        eval_dataset,
        collate_fn=default_data_collator,
        batch_size=batch_size,
        pin_memory=True,
    )

    if not os.path.exists(args.checkpoint_path):
        os.makedirs(args.checkpoint_path, exist_ok=True)

    model = AutoModelForCausalLM.from_pretrained(args.model_name_or_path)
    model.resize_token_embeddings(len(tokenizer))

    print("model summary:\n", model)

    peft_config = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        target_modules=["c_proj"],
        fan_in_fan_out=True,
        inference_mode=False,
        r=8,
        lora_alpha=32,
        lora_dropout=0.1,
    )

    model = get_peft_model(model, peft_config)
    model.print_trainable_parameters()

    # Optimizer and LR-Scheduler
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate)
    lr_scheduler = get_linear_schedule_with_warmup(
        optimizer=optimizer,
        num_warmup_steps=0,
        num_training_steps=(len(train_dataloader) * args.num_epochs),
    )

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    args.n_gpu = torch.cuda.device_count()
    print("Num of gpu(s):", args.n_gpu)
    if args.n_gpu > 1:
        model = nn.DataParallel(model)
    model = model.to(device)
    model.zero_grad()

    if args.max_steps > 0:
        t_total = args.max_steps
        args.num_epochs = args.max_steps // (len(train_dataloader) // args.gradient_accumulation_steps) + 1
    else:
        t_total = len(train_dataloader) // args.gradient_accumulation_steps * args.num_epochs

    # Training
    logging.info("==========Start training==========")
    logging.info(
        "Total train batch size (w. parallel, distributed & accumulation) = %d",
        batch_size * args.gradient_accumulation_steps,
    )
    logging.info("Gradient Accumulation steps = %d", args.gradient_accumulation_steps)
    logging.info("Total optimization steps = %d", t_total)

    comparative_loss = -1

    global_steps, eval_steps, total_loss, eval_loss, logging_loss, eval_logging_loss = 0, 0, 0, 0, 0, 0
    for epoch in tqdm(range(args.num_epochs), position=0, desc="Epoch", leave=False):
        for step, batch in enumerate(tqdm(train_dataloader, position=1, desc="Training", leave=False)):
            model.train()

            batch = {k: v.to(device) for k, v in batch.items()}

            inputs = {"attention_mask": batch["attention_mask"], "labels": batch["labels"]}

            if args.n_gpu > 1:
                embeds_init = model.module.transformer.wte(batch["input_ids"])
            else:
                embeds_init = model.transformer.wte(batch["input_ids"])

            if args.adv_init_mag > 0:
                input_mask = inputs["attention_mask"].to(embeds_init)
                input_lengths = torch.sum(input_mask, 1)

                if args.norm_type == "l2":
                    delta = torch.zeros_like(embeds_init).normal_(0, 1) * input_mask.unsqueeze(2)
                    dims = input_lengths * embeds_init.size(-1)
                    mag = args.adv_init_mag / torch.sqrt(dims)
                    delta = (delta * mag.view(-1, 1, 1)).detach()
            else:
                delta = torch.zeros_like(embeds_init)

            outputs = model(**batch)

            # The K ascent steps
            for astep in range(args.adv_steps):
                # [1] Forward propagation
                delta.requires_grad_()
                inputs["inputs_embeds"] = delta + embeds_init

                adv_outputs = model(**inputs)
                adv_loss = adv_outputs.loss

                mse_loss = F.mse_loss(adv_outputs.logits, outputs.logits.detach(), reduction="mean")
                adv_loss = adv_loss + args.adv_smooth * mse_loss

                if args.n_gpu > 1:
                    adv_loss = adv_loss.mean()  # mean() to average on multi-gpu parallel training
                if args.gradient_accumulation_steps > 1:
                    adv_loss = adv_loss / args.gradient_accumulation_steps

                adv_loss = adv_loss / args.adv_steps

                total_loss += adv_loss.item()

                # [2] Backward propagation
                if args.n_gpu > 1:
                    adv_loss.sum().backward(retain_graph=True)
                else:
                    adv_loss.backward(retain_graph=True)

                if astep == args.adv_steps - 1:
                    break

                # [3] Get gradient on delta of divergence loss
                (delta_grad,) = torch.autograd.grad(mse_loss, delta, create_graph=True)

                # [4] Calculate gradients
                if args.norm_type == "l2":
                    denorm = torch.norm(delta_grad.view(delta_grad.size(0), -1), dim=1).view(-1, 1, 1)
                    denorm = torch.clamp(denorm, min=1e-8)

                    if epoch < args.num_epochs - 2:
                        # Gradient Ascent Step
                        delta = (delta + args.adv_lr * delta_grad / denorm).detach()

                        # Projected Gradient Descent
                        if args.adv_max_norm > 0:
                            delta_norm = torch.norm(delta.view(delta.size(0), -1).float(), p=2, dim=1).detach()
                            exceed_mask = (delta_norm > args.adv_max_norm).to(embeds_init)
                            reweights = (args.adv_max_norm / delta_norm * exceed_mask + (1 - exceed_mask)).view(
                                -1, 1, 1
                            )
                            delta = (delta * reweights).detach()

                    else:
                        # Newton-like step
                        (hessian_matrix,) = torch.autograd.grad(delta_grad.sum(), delta)
                        inverse_sqmatrix = torch.linalg.inv(
                            torch.matmul(hessian_matrix, torch.transpose(hessian_matrix, 1, 2))
                        )
                        inverse_delta = torch.matmul(torch.transpose(hessian_matrix, 1, 2), inverse_sqmatrix)

                        # Gradient Ascent Step
                        delta = (
                            delta + args.adv_lr * torch.transpose(inverse_delta, 1, 2) * delta_grad / denorm
                        ).detach()

                        # Projected-Newton Method
                        if args.adv_max_norm > 0:
                            delta_norm = torch.norm(delta.view(delta.size(0), -1).float(), dim=1).detach()
                            exceed_mask = (delta_norm > args.adv_max_norm).to(embeds_init)
                            diff = (args.adv_max_norm / delta_norm * exceed_mask + (1 - exceed_mask)).view(-1, 1, 1)
                            reweights = diff * hessian_matrix * diff
                            delta = (delta * reweights).detach()

            if (step + 1) % args.gradient_accumulation_steps == 0 or global_steps > len(train_dataloader):
                torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)
                optimizer.step()
                lr_scheduler.step()
                # optimizer.zero_grad()
                model.zero_grad()
                global_steps += 1

        train_epoch_loss = (total_loss - logging_loss) / len(train_dataloader)
        print("\naverage train_loss:", train_epoch_loss)
        logging_loss = total_loss

        # Evaluation
        f = open(f"output_at_{epoch}.txt", "w", encoding="utf-8")
        for step, batch in enumerate(tqdm(eval_dataloader, position=1, desc="Validation", leave=False)):
            model.eval()
            batch = {k: v.to(device) for k, v in batch.items()}

            inputs = {"attention_mask": batch["attention_mask"], "labels": batch["labels"]}

            if args.n_gpu > 1:
                embeds_init = model.module.transformer.wte(batch["input_ids"])
            else:
                embeds_init = model.transformer.wte(batch["input_ids"])

            if args.adv_init_mag > 0:
                input_mask = inputs["attention_mask"].to(embeds_init)
                input_lengths = torch.sum(input_mask, 1)

                if args.norm_type == "l2":
                    delta = torch.zeros_like(embeds_init).normal_(0, 1) * input_mask.unsqueeze(2)
                    dims = input_lengths * embeds_init.size(-1)
                    mag = args.adv_init_mag / torch.sqrt(dims)
                    delta = (delta * mag.view(-1, 1, 1)).detach()
            else:
                delta = torch.zeros_like(embeds_init)

            inputs["inputs_embeds"] = delta + embeds_init

            with torch.no_grad():
                outputs = model(**inputs)
                tmp_eval_loss = outputs.loss
                if args.n_gpu > 1:
                    tmp_eval_loss = tmp_eval_loss.mean()
                if args.gradient_accumulation_steps > 1:
                    tmp_eval_loss = tmp_eval_loss / args.gradient_accumulation_steps

                eval_loss += tmp_eval_loss.item()

            if (step + 1) % args.gradient_accumulation_steps == 0 or args.gradient_accumulation_steps > len(
                eval_dataloader
            ):
                eval_steps += 1

            results = tokenizer.batch_decode(
                torch.argmax(outputs.logits, -1).detach().cpu().numpy(), skip_special_tokens=True
            )
            input_batch = tokenizer.batch_decode(batch["input_ids"].detach().cpu().numpy(), skip_special_tokens=True)
            label_batch = tokenizer.batch_decode(
                batch["labels"][batch["labels"] != -100].unsqueeze(0).detach().cpu().numpy(), skip_special_tokens=True
            )

            for res, inp, lab in zip(results, input_batch, label_batch):
                f.write("Input:" + inp)
                f.write("\n")
                f.write("Label:" + lab)
                f.write("\n")
                f.write("Result:" + res)
                f.write("\n\n")
        f.close()

        eval_epoch_loss = (eval_loss - eval_logging_loss) / len(eval_dataloader)
        eval_logging_loss = eval_loss

        logging.info(
            f"Epoch {epoch+1}: \
                    train_loss: {train_epoch_loss}, \
                    valid_loss: {eval_epoch_loss}"
        )

        print("\naverage eval_loss:", eval_epoch_loss)

        cummulative_loss = eval_epoch_loss

        if comparative_loss == -1 or cummulative_loss < comparative_loss:
            # Update comparative_loss for later comparison
            comparative_loss = cummulative_loss

            # Saving model
            logging.info(f"Epoch {epoch+1}: Saving model and tokenizer...")

            model_path = os.path.join(args.checkpoint_path, f"model_{epoch}.pt")
            torch.save({"model_state_dict": model.state_dict()}, model_path)
            tokenizer.save_pretrained(args.checkpoint_path)

            logging.info(f"Epoch {epoch+1}: Done.")

    logging.info(
        f"Final Summary: \
                    train_loss: {total_loss / global_steps}, \
                    valid_loss: {eval_loss / eval_steps}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--train_data_path", type=str, default="./data/quora/QQP_split/train_ready.txt", help="train dataset file"
    )
    parser.add_argument(
        "--valid_data_path", type=str, default="./data/quora/QQP_split/valid_ready.txt", help="valid dataset file"
    )

    parser.add_argument(
        "--input_file", type=str, default="./data/quora/QQP_split/train.txt", help="input dataset file"
    )
    parser.add_argument(
        "--output_file", type=str, default="./data/quora/QQP_split/train_ready.txt", help="output dataset file"
    )

    parser.add_argument("--log", type=str, default="./logs/train_{datetime}.log", help="Log filename")
    parser.add_argument("--device", type=str, default="cuda", help="{cuda, cpu}")

    parser.add_argument("--model_name_or_path", type=str, default="ai-forever/mGPT", help="pretrained model name")
    parser.add_argument("--max_length", type=int, default=64, help="Maximum number of tokens for each sequence")
    parser.add_argument("--batch_size", type=int, default=4, help="Training batch size")
    parser.add_argument("--eval_batch_size", type=int, default=4, help="Evaluation batch size")
    parser.add_argument("--checkpoint_path", type=str, default="checkpoint/", help="checkpoint path to save model")
    parser.add_argument("--local_rank", type=int, default=0, help="local rank")

    parser.add_argument("--learning_rate", type=float, default=2e-5, help="Learning rate of fine-tuning")
    parser.add_argument("--num_epochs", type=int, default=10, help="Number of epochs")
    parser.add_argument("--train_size", type=int, default=200000)

    parser.add_argument("--max_grad_norm", default=1.0, type=float, help="Max gradient norm.")

    parser.add_argument("--debug", type=bool, default=True)
    parser.add_argument("--seed", type=int, default=2023)
    parser.add_argument("--type", type=str, default="train")

    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=64,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument(
        "--max_steps",
        default=-1,
        type=int,
        help="If > 0: set total number of training steps to perform. Override num_train_epochs.",
    )

    # ========================= ADVERSARIAL CONFIGURATION =========================
    parser.add_argument("--adv_lr", type=float, default=2e-5)
    parser.add_argument("--adv_steps", type=int, default=2, help="should be at least 1")
    parser.add_argument("--adv_init_mag", type=float, default=1)
    parser.add_argument("--norm_type", type=str, default="l2")
    parser.add_argument("--adv_max_norm", type=float, default=2e-5, help="set to 0 to be unlimited")
    parser.add_argument("--adv_smooth", type=float, default=1)
    # ===========================================================================

    # ========================= LoRA CONFIGURATION ==============================
    parser.add_argument("--lora_dim", type=int, default=8, help="lora attn dimension")
    parser.add_argument("--lora_alpha", type=int, default=128, help="lora attn alpha")
    parser.add_argument("--lora_dropout", default=0.1, type=float, help="dropout probability for lora layers")
    parser.add_argument("--label_smooth", default=0.1, type=float, help="label smoothing")
    parser.add_argument(
        "--init_checkpoint", default="pretrained_checkpoints/pytorch_model.bin", help="pretrained checkpoint path"
    )
    # ===========================================================================

    args = parser.parse_args()

    if not os.path.exists("logs/"):
        os.mkdir("logs/")

    log_format = "%(asctime)s [%(levelname)s] %(message)s"
    log_level = logging.DEBUG if args.debug else logging.INFO
    log_file = args.log

    logging.basicConfig(
        level=log_level, format=log_format, filename=log_file.format(datetime=start_datetime.replace(":", "-"))
    )
    logging.getLogger().setLevel(log_level)

    # Reproducibility
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    logging.info(f"Parsed args: {json.dumps(dict(args.__dict__), indent=2)}")

    main(args)
