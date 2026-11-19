"""Download the embedding model into the image at build time (WITH_ST=1) so containers need no internet at run time."""
import os
import sys

name = os.environ.get("EMBEDDING_MODEL", "").strip()
if not name:
    sys.exit("EMBEDDING_MODEL is empty: set it in .env (the model named in section 12 of the course brief) before building with WITH_ST=1")
from sentence_transformers import SentenceTransformer

m = SentenceTransformer(name)
print("prefetched", name, "dim", m.get_sentence_embedding_dimension())
