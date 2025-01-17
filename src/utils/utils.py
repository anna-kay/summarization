from tqdm import tqdm
from evaluate import load
from nltk.tokenize import sent_tokenize
from sentence_transformers import SentenceTransformer, util
import argparse
import json
import os
import statistics

import torch
import torch.optim as optim

import numpy as np
import matplotlib.pyplot as plt

import nltk
nltk.download('punkt')
nltk.download('punkt_tab')


def get_parser():

    parser = argparse.ArgumentParser()

    # parser.add_argument("--path", type=str, default = "data/APTNER_processed/", help="Directory of the data")
    parser.add_argument("--data_dir", type=str,
                        default="data", help="Directory of the data")
    parser.add_argument("--dataset_dir", type=str,
                        default="webis_tldr_mini", help="Directory of the dataset")
    parser.add_argument("--train_dataset_dir", type=str, default="webis_tldr_mini_train",
                        help="Directory for the train split of the dataset")
    parser.add_argument("--val_dataset_dir", type=str, default="webis_tldr_mini_val",
                        help="Directory for the validation split of the dataset")
    parser.add_argument("--test_dataset_dir", type=str, default="webis_tldr_mini_test",
                        help="Directory for the test split of the dataset")
    parser.add_argument("--checkpoint", type=str,
                        default="microsoft/prophetnet-large-uncased", help="Hugging Face model checkpoint")
    parser.add_argument("--do_lower_case", type=bool, default=False,
                        help="True if the model is uncased, should be defined according to checkpoint")
    parser.add_argument("--max_source_length", type=int, default=512,
                        help="Maximal number of tokens per sequence. All sequences will be cut or padded to this length.")
    parser.add_argument("--max_target_length", type=int, default=128,
                        help="Maximal number of tokens per sequence. All sequences will be cut or padded to this length.")
    parser.add_argument("--batch_size", type=int,
                        default=16, help="Batch size")
    parser.add_argument("--max_grad_norm", type=float,
                        default=1.0, help="Maximum gradient norm")
    parser.add_argument("--epochs", type=int, default=4,
                        help="Number of epochs")
    parser.add_argument("--learning_rate", type=float,
                        default=1e-6, help="Learning rate")
    parser.add_argument("--epsilon", type=float, default="1e-12",
                        help="Epsilon prevents division by zero.")
    # parser.add_argument("--num_beams", type=int, default=4,
    #                     help="Decoding beam search size.")
    # parser.add_argument("--early_stopping", type=bool, default="True",
    #                     help="If True decoding stops when the EOS token is reached")
    parser.add_argument("--wandb_project", type=str,
                        default="Abstractive Summarization", help="Wandb project name")
    parser.add_argument("--wandb_entity", type=str,
                        default="anna-kay", help="Wandb entity name")

    return parser


# -------------------------------- TRAINING UTILS -------------------------------- #

def get_optimizer(model, learning_rate, epsilon):

    param_optimizer = list(model.named_parameters())

    no_decay = ['bias', 'gamma', 'beta']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)],
         'weight_decay_rate': 0.01},
        {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)],
         'weight_decay_rate': 0.0}
    ]

    optimizer = optim.AdamW(optimizer_grouped_parameters,
                            lr=learning_rate, eps=epsilon)

    return optimizer


def train_epoch(model, epoch, train_loader, optimizer, max_grad_norm, scheduler, device, wandb):

    model.train()
    train_loss = 0.0

    for batch in tqdm(train_loader, desc=f"{epoch+1}"):
        input_ids = batch["input_ids"].to(device).long()
        attention_mask = batch["attention_mask"].to(device).long()
        labels = batch["labels"].to(device).long()

        # Zero gradients
        optimizer.zero_grad()

        outputs = model(input_ids=input_ids,
                        # token_type_ids=None,
                        attention_mask=attention_mask,
                        labels=labels)

        loss = outputs.loss
        loss.backward()
        train_loss += loss.item()

        torch.nn.utils.clip_grad_norm_(
            parameters=model.parameters(), max_norm=max_grad_norm)

        optimizer.step()
        scheduler.step()

    # Calculate and log average training loss and learning rate for the epoch
    avg_train_loss = train_loss / len(train_loader)
    current_lr = scheduler.get_last_lr()[0]

    wandb.log({"epoch": epoch+1, "train_loss": avg_train_loss})
    wandb.log({"epoch": epoch+1, "learning_rate": current_lr})

    return avg_train_loss, current_lr


def train_epoch_manually_compute_grads(model, epoch, train_loader, max_grad_norm, learning_rate, device, wandb):

    model.train()
    train_loss = 0.0

    for batch in tqdm(train_loader, desc=f"{epoch+1}"):
        input_ids = batch["input_ids"].to(device).long()
        attention_mask = batch["attention_mask"].to(device).long()
        labels = batch["labels"].to(device).long()

        # Zero gradients
        model.zero_grad()

        outputs = model(input_ids=input_ids,
                        attention_mask=attention_mask,
                        labels=labels)

        loss = outputs.loss
        loss.backward()
        train_loss += loss.item()

        torch.nn.utils.clip_grad_norm_(
            parameters=model.parameters(), max_norm=max_grad_norm)

        # Manually update model parameters
        with torch.no_grad():
            for param in model.parameters():
                param -= learning_rate * param.grad

    # Calculate and log average training loss and learning rate for the epoch
    avg_train_loss = train_loss / len(train_loader)

    wandb.log({"epoch": epoch+1, "train_loss": avg_train_loss})
    # wandb.log({"epoch": epoch+1, "learning_rate": current_lr})

    return avg_train_loss


def evaluate_epoch(model, tokenizer, epoch, val_loader, device, generation_config, wandb):
    model.eval()
    val_loss = 0
    predictions, true_labels = [], []

    with torch.no_grad():
        for batch in tqdm(val_loader, desc=f"Epoch {epoch+1}"):
            input_ids = batch["input_ids"].to(device).long()
            attention_mask = batch["attention_mask"].to(device).long()
            labels = batch["labels"].to(device).long()

            outputs = model(input_ids=input_ids,
                            # token_type_ids=None,
                            attention_mask=attention_mask,
                            labels=labels)

            # logits = outputs.logits.detach().cpu().numpy()
            logits = outputs.logits.to('cpu').numpy()
            # .detach() is redundant
            # TODO: Ensure that your model’s logits are in the shape (batch_size, sequence_length, vocab_size).
            label_ids = labels.to('cpu').numpy()

            val_loss += outputs.loss.item()  # outputs.loss.mean().item()

            # Use model.generate() with beam search decoding to generate predictions for summaries
            generated_ids = model.generate(
                input_ids=input_ids,
                attention_mask=attention_mask,
                generation_config=generation_config
            )

            # Decode the generated sequences to text
            decoded_preds = tokenizer.batch_decode(
                generated_ids, skip_special_tokens=True)

            # Replace -100 in the labels as they cannot be decoded
            label_ids = np.where(
                label_ids != -100, label_ids, tokenizer.pad_token_id)
            decoded_labels = tokenizer.batch_decode(
                label_ids, skip_special_tokens=True)

            # Append to the list for ROUGE score calculation
            predictions.extend(decoded_preds)
            true_labels.extend(decoded_labels)

            # # Compute predicted labels from logits -> This give the summaries that would be generated using greedy decoding
            # batch_predictions = np.argmax(logits, axis=2)
            # predictions.extend(batch_predictions.tolist())
            # true_labels.extend(label_ids.tolist())

    avg_val_loss = val_loss/len(val_loader)
    wandb.log({"epoch": epoch+1, "val_loss": avg_val_loss})

    return avg_val_loss, predictions, true_labels


def save_best_model(model, epoch_count, folder, best_metric):

    best_model_folder = os.path.join(".", folder, "best_model")
    os.makedirs(best_model_folder, exist_ok=True)

    # Save the best model checkpoint
    model.save_pretrained(best_model_folder, "best_model")

    # Store epoch number & best scores
    best_model_info = {
        "epoch": epoch_count,
        "best_metric": best_metric
    }

    with open(os.path.join(".", folder, "best_model_info.json"), "a") as outfile:
        json.dump(best_model_info, outfile, indent=4)

    return None


# -------------------------------- PLOTTING UTILS -------------------------------- #

def plot_train_val_losses(train_loss_values, val_loss_values, epochs):

    x = range(1, epochs+1)

    plt.title("Training & Validation Losses")
    plt.xlabel("Epoch")
    plt.ylabel("Loss")

    plt.xticks(x)
    plt.plot(x, train_loss_values, marker='o', label='train loss')
    plt.plot(x, val_loss_values, marker='o', label='valid loss')
    plt.legend()
    plt.grid(linestyle='--')
    plt.show()


# -------------------------------- METRICS EVALUATION UTILS -------------------------------- #

def calculate_metrics(predictions, labels):

    rouge_scores = calculate_rouge_metrics(predictions, labels)

    semantic_similarity_scores = calculate_semantic_similarity(predictions, labels)

    BERTScore = calculate_BERTScore(predictions, labels)

    return {
            "ROUGE": rouge_scores,
            "Semantic similarity": semantic_similarity_scores,
            "BERTScore": BERTScore
            }


def calculate_rouge_metrics(predictions, labels):

    rouge_score = load("rouge")

    # decoded_preds = ["\n".join(sent_tokenize(pred.strip()))
    #                  for pred in predictions]
    # decoded_labels = ["\n".join(sent_tokenize(label.strip()))
    #                   for label in labels]
    
    # ROUGE expects a newline after each sentence
    # [X_SEP] is a separator used in ProphetNet 
    decoded_preds = ["\n".join(sent_tokenize(pred.replace('[X_SEP]', ' ' ).strip())) for pred in predictions]
    decoded_labels = ["\n".join(sent_tokenize(label.replace(" .", ".").strip())) for label in labels]

    result = rouge_score.compute(
        predictions=decoded_preds, references=decoded_labels, use_stemmer=True
    )

    # Extract the median scores
    result = {key: value * 100 for key, value in result.items()}

    return {k: round(v, 3) for k, v in result.items()}


def calculate_semantic_similarity(predictions, labels):
    # Computation of Semantic Similarity using SBERT
    # Step 1: Load the pre-trained model
    semantic_similarity_model = SentenceTransformer('all-mpnet-base-v2')
    # semantic_similarity_model = SentenceTransformer('all-MiniLM-L6-v2')

    semantic_similarities = []
    semantic_similarity_min = float("inf")

    # Step 2: Define the terms
    for sent1, sent2 in zip(predictions, labels):

        # Step 3: Encode the terms into embeddings
        embedding1 = semantic_similarity_model.encode(sent1, convert_to_tensor=True)
        embedding2 = semantic_similarity_model.encode(sent2, convert_to_tensor=True)

        # Step 4: Compute the cosine similarity
        similarity = float(util.cos_sim(embedding1, embedding2))

        if similarity < semantic_similarity_min:
            semantic_similarity_min = similarity

        semantic_similarities.append(similarity)        

    semantic_similarity_avg = sum(semantic_similarities)/len(semantic_similarities)

    return {
            "semantic_similarity_avg": semantic_similarity_avg,
            "semantic_similarity_min": semantic_similarity_min
            } 


def calculate_BERTScore(predictions, labels):

    # BERTScore 
    bertscore = load("bertscore")
    bertscore_metrics = bertscore.compute(predictions=predictions, references=labels, lang="en")

    bertscore_metrics_avgs = {"precision": statistics.mean(bertscore_metrics["precision"]),
                              "recall": statistics.mean(bertscore_metrics["recall"]),
                              "f1": statistics.mean(bertscore_metrics["f1"])}

    return bertscore_metrics_avgs


def print_out_predictions_labels(predictions, labels):

    with open('ground_truth_predictions.txt', 'w') as f:
        for i in range(len(labels)):
            print(f"Example {i+1}", file=f)
            print(f"Ground truth: {labels[i]}", file=f)
            print(f"Prediction: {predictions[i]}", file=f)
            print("-" * 40, file=f)  





