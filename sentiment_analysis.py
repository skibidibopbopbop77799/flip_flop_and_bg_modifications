import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
import torch.nn.utils.rnn as rnn_utils
import pandas as pd
import re
import random
import math
from tqdm import tqdm
from jk_flip_flop import FF
# Check if GPU is available
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def load_data(f):
    df = pd.read_csv(f)
    return df

def cleaner(doc):
    doc = doc.lower()
    doc = doc.replace("\n", " ")
    doc = re.sub("<br />", " ", doc)
    doc = re.sub(".*\.html$", " ", doc)
    doc = re.sub("[-;—:&%$()/*^\[\]\{\}£]", " ", doc)
    doc = re.sub("[_#]", "", doc)
    doc = re.sub("[“]", " ", doc)
    doc = re.sub("[,]", " ", doc)
    doc = re.sub("[?]", " ", doc)
    doc = re.sub("[\"]", " ", doc)
    doc = re.sub("[!]", " ", doc)
    doc = re.sub("[0-9]+", "<NUM>", doc)
    doc = re.sub("dr\.", "dr", doc)
    doc = re.sub("u\.s\.", "us", doc)
    doc = re.sub("u\.s\.a\.", "usa", doc)
    doc = re.sub("[.]", " ", doc)
    return doc

def Tokenizer(doc):
    words = doc.split(' ')
    real_words = [word for word in words if word]
    return real_words

def preprocess_data(f):
    df = load_data(f)
    reviews = [[Tokenizer(cleaner(df['review'][i])), 1 if df['sentiment'][i] == "positive" else 0] for i in range(len(df))]
    return reviews

def map_words_to_ids(sentences):
    unique_words = {"<pad>": 0}
    for sentence in sentences:
        for word in sentence[0]:  # Accessing the tokenized review
            if word not in unique_words:
                unique_words[word] = len(unique_words)
    unique_words["<unk>"] = len(unique_words)
    return unique_words

def train_val_test_split(sentences):
    random.shuffle(sentences)
    num_train = int(0.7 * len(sentences))
    num_val = int(0.2 * len(sentences))
    train_sentences = sentences[:num_train]
    val_sentences = sentences[num_train:num_train + num_val]
    test_sentences = sentences[num_train + num_val:]
    return train_sentences, val_sentences, test_sentences
def find_max_seq_len(data_loader):
    max_length = 0
    for texts, labels, lengths in data_loader:
        batch_max_length = lengths[0]  
        if batch_max_length > max_length:
            max_length = batch_max_length
    return max_length
class IMDBDataset(Dataset):
    def __init__(self, texts, word_to_idx):
        self.texts = [torch.tensor([word_to_idx.get(word, word_to_idx["<unk>"]) for word in text[0]], dtype=torch.long) for text in texts]
        self.targets = torch.tensor([text[1] for text in texts], dtype=torch.long)

    def __len__(self):
        return len(self.texts)

    def __getitem__(self, idx):
        return self.texts[idx], self.targets[idx]

def collate_fn(batch):
    texts, labels = zip(*batch)
    lengths = [len(text) for text in texts]
    texts_padded = rnn_utils.pad_sequence(texts, batch_first=True, padding_value=0)
    labels = torch.tensor(labels)

    # Sort by lengths for potential RNN handling
    lengths, sorted_idx = torch.tensor(lengths).sort(descending=True)
    texts_padded = texts_padded[sorted_idx]
    labels = labels[sorted_idx]
    
    return texts_padded, labels, lengths

class HRNN_FF(nn.Module):
    def __init__(self, vocab_size, embedding_dim, hidden_units, chunk_units, output_dim, max_seq_len, chunk_size):
        super(HRNN_FF, self).__init__()
        
        self.chunk_size = chunk_size
        self.hidden_units = hidden_units

        # Embedding layer
        self.embedding = nn.Embedding(vocab_size, embedding_dim)
        
        # Lower-level flip-flop layers for chunk processing
        self.ff_chunk_fw = nn.ModuleList([FF(chunk_units) for _ in range(chunk_size)])
        self.ff_chunk_bw = nn.ModuleList([FF(chunk_units) for _ in range(chunk_size)])
        
        # Higher-level layers for sentence/paragraph-level processing
        self.ff_sentence_fw = nn.ModuleList([FF(hidden_units) for _ in range(max_seq_len // chunk_size)])
        self.ff_sentence_bw = nn.ModuleList([FF(hidden_units) for _ in range(max_seq_len // chunk_size)])
        
        # Dense layer for final output
        self.dense = nn.Linear(2 * hidden_units, output_dim)
        
    def forward(self, input_sequence):
        # Embed and chunk the input sequence
        embedded = self.embedding(input_sequence)
        batch_size, seq_len, _ = embedded.size()
        num_chunks = seq_len // self.chunk_size

        # Process each chunk independently using the lower-level flip-flop layers
        chunk_outputs = []
        for i in range(num_chunks):
            chunk = embedded[:, i * self.chunk_size:(i + 1) * self.chunk_size, :]
            hidden_fw = torch.zeros(batch_size, self.ff_chunk_fw[0].units).to(input_sequence.device)
            hidden_bw = torch.zeros(batch_size, self.ff_chunk_bw[0].units).to(input_sequence.device)

            outputs_fw, outputs_bw = [], []
            for t in range(self.chunk_size):
                hidden_fw = self.ff_chunk_fw[t](chunk[:, t, :], hidden_fw)
                outputs_fw.append(hidden_fw)
            for t in reversed(range(self.chunk_size)):
                hidden_bw = self.ff_chunk_bw[t](chunk[:, t, :], hidden_bw)
                outputs_bw.append(hidden_bw)

            # Concatenate the forward and backward outputs for the chunk
            outputs_fw = torch.stack(outputs_fw, dim=1)
            outputs_bw = torch.stack(outputs_bw, dim=1)
            combined_output = torch.cat((outputs_fw, outputs_bw), dim=2)
            
            # Pool chunk output and add to chunk_outputs list
            chunk_outputs.append(torch.mean(combined_output, dim=1))
        
        # Combine all chunk representations into a higher-level sequence
        sentence_sequence = torch.stack(chunk_outputs, dim=1)
        # print(f"sent seq shape: {sentence_sequence.shape}")
        # Higher-level processing of sentence sequence using sentence flip-flop layers
        hidden_fw = torch.zeros(batch_size, self.ff_sentence_fw[0].units).to(input_sequence.device)
        hidden_bw = torch.zeros(batch_size, self.ff_sentence_bw[0].units).to(input_sequence.device)
        
        sentence_outputs_fw, sentence_outputs_bw = [], []
        for t in range(sentence_sequence.size(1)):
            hidden_fw = self.ff_sentence_fw[t](sentence_sequence[:, t, :], hidden_fw)
            sentence_outputs_fw.append(hidden_fw)
        for t in reversed(range(sentence_sequence.size(1))):
            hidden_bw = self.ff_sentence_bw[t](sentence_sequence[:, t, :], hidden_bw)
            sentence_outputs_bw.append(hidden_bw)

        # Concatenate forward and backward sentence outputs
        sentence_outputs_fw = torch.stack(sentence_outputs_fw, dim=1)
        sentence_outputs_bw = torch.stack(sentence_outputs_bw, dim=1)
        combined_sentence_output = torch.cat((sentence_outputs_fw, sentence_outputs_bw), dim=2)
        
        # Pooling for final sentence representation
        final_output = torch.mean(combined_sentence_output, dim=1)
        # Dense layer for classification
        logits = self.dense(final_output)
        return logits

def train_model(model, data_loader, loss_fn, optimizer, num_epochs=10):
    model.train()
    model.to(device)  # Move model to the device
    for epoch in range(num_epochs):
        total_loss = 0
        for texts, labels, lengths in tqdm(data_loader):
            texts, labels = texts.to(device), labels.to(device)  # Move data to device
            optimizer.zero_grad()
            outputs = model(texts)
            loss = loss_fn(outputs, labels)
            loss.backward()
            optimizer.step()
            total_loss += loss.item()
        print(f"Epoch {epoch + 1}/{num_epochs}, Loss: {total_loss / len(data_loader)}")

def evaluate_model(model, data_loader, loss_fn):
    model.eval()
    model.to(device)  # Move model to the device
    total_loss, correct_predictions = 0, 0
    with torch.no_grad():
        for texts, labels, lengths in tqdm(data_loader):
            texts, labels = texts.to(device), labels.to(device)  # Move data to device
            outputs = model(texts)
            loss = loss_fn(outputs, labels)
            total_loss += loss.item()
            predictions = torch.argmax(outputs, dim=1)
            correct_predictions += (predictions == labels).sum().item()
    accuracy = correct_predictions / len(data_loader.dataset)
    avg_loss = total_loss / len(data_loader)
    return avg_loss, accuracy

# Load data and preprocess
reviews = preprocess_data('IMDB_Dataset.csv')
train_reviews, val_reviews, test_reviews = train_val_test_split(reviews)
vocab = map_words_to_ids(train_reviews)

# Datasets and Dataloaders
train_dataset = IMDBDataset(train_reviews, vocab)
val_dataset = IMDBDataset(val_reviews, vocab)
test_dataset = IMDBDataset(test_reviews, vocab)

train_loader = DataLoader(train_dataset, batch_size=128, shuffle=True, collate_fn=collate_fn)
val_loader = DataLoader(val_dataset, batch_size=32, shuffle=False, collate_fn=collate_fn)
test_loader = DataLoader(test_dataset, batch_size=32, shuffle=False, collate_fn=collate_fn)


max_train = find_max_seq_len(train_loader)
max_val = find_max_seq_len(val_loader)
max_test = find_max_seq_len(test_loader)
# Model, Loss, and Optimizer
vocab_size = len(vocab)
embedding_dim = 100
chunk_units = 100
hidden_units = 2 * chunk_units
chunk_size = 5
output_dim = 2
max_seq_len = max(max_train, max_val, max_test)

model = HRNN_FF(vocab_size, embedding_dim, hidden_units, chunk_units, output_dim, max_seq_len, chunk_size).to(device)  # Move model to device
loss_fn = nn.CrossEntropyLoss()
optimizer = torch.optim.Adam(model.parameters(), lr=0.001)

# Training and validation
train_model(model, train_loader, loss_fn, optimizer, num_epochs=10)
val_loss, val_accuracy = evaluate_model(model, val_loader, loss_fn)
print(f"Validation Loss: {val_loss:.4f}, Validation Accuracy: {val_accuracy:.4f}")

# Testing
test_loss, test_accuracy = evaluate_model(model, test_loader, loss_fn)
print(f"Test Loss: {test_loss:.4f}, Test Accuracy: {test_accuracy:.4f}")
torch.save(model.state_dict(), 'hrnn_flipflop.pt')
