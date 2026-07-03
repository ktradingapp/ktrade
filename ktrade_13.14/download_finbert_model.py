"""Download and test the FinBERT model before starting the dashboard backend.
Run this once. It may take several minutes because the model is large.
"""
from transformers import pipeline

print("Downloading/loading ProsusAI/finbert. This can take time on first run...")
pipe = pipeline("sentiment-analysis", model="ProsusAI/finbert")
print("Model loaded. Running test...")
print(pipe(["The company reported strong revenue growth and raised guidance."]))
print("SUCCESS: FinBERT is downloaded and working.")
