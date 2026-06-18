from sentence_transformers import SentenceTransformer, InputExample
from sentence_transformers.sentence_transformer import losses
from torch.utils.data import DataLoader
import json

def fine_tune():
    with open('data/rbi_training_pairs.json') as f:
        data = json.load(f)
    
    examples = [InputExample(texts=[d['guideline'], d['clause']], label=float(d['label'])) for d in data]
    
    model = SentenceTransformer('all-MiniLM-L6-v2')
    dataloader = DataLoader(examples, shuffle=True, batch_size=8)
    loss = losses.CosineSimilarityLoss(model)
    
    model.fit(train_objectives=[(dataloader, loss)], epochs=5, warmup_steps=50)
    model.save('models/fine_tuned_rbi')
    print("Model saved!")

fine_tune()