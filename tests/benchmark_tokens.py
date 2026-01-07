import time
import sys
import os

sys.path.append(os.getcwd())

from utils.context_manager import count_tokens

def benchmark_token_counting():
    text = "The quick brown fox jumps over the lazy dog. " * 100
    
    # Verify method
    try:
        import tiktoken
        print("Tiktoken is installed.")
    except ImportError:
        print("Tiktoken is NOT installed. Using fallback.")

    start_time = time.time()
    for _ in range(1000):
        count_tokens(text)
    end_time = time.time()
    
    print(f"Time for 1000 calls: {end_time - start_time:.4f} seconds")

if __name__ == "__main__":
    benchmark_token_counting()
