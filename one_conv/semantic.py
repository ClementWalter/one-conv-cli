"""Embed text locally with a static (model2vec) embedding model: a WordPiece tokenizer and one vector per token.

The model is minishlab/potion-retrieval-32M, pinned by revision and checksum. Encoding is a lookup and
a mean, so a laptop embeds thousands of turns per second without a GPU, torch or the tokenizers package.
"""

import hashlib
import json
import logging
import os
import struct
import unicodedata
import urllib.request
from pathlib import Path

import numpy as np

log = logging.getLogger("one-conv")

MODEL = "minishlab/potion-retrieval-32M"
REVISION = "6fc8051fab2a1e0ee76689cf08c853792ac285e7"
# Public SHA-256 digests of the published files.
FILES = {"tokenizer.json": "7d75cbc54318138807c401b0f0c9721117c628b39de8e8e0edb6cb17e0ee7d18",  # privacy-guard: allow
         "model.safetensors": "07609e5bd33aad37900b3fd62f4ec96f6daec88ca4d46b9d8b928bfababf6ea0"}  # privacy-guard: allow
BASE_URL = f"https://huggingface.co/{MODEL}/resolve/{REVISION}"
# model2vec's default: longer texts are truncated to their first 512 tokens.
MAX_TOKENS = 512
DTYPES = {"F32": np.float32, "F16": np.float16}


def fetch(directory, base_url=BASE_URL):
    """The directory holding the model files, downloading and checking each one missing."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, digest in FILES.items():
        target = directory / name
        if target.exists():
            continue
        log.warning("Downloading the semantic search model file %s (once).", name)
        partial = target.with_suffix(target.suffix + f".{os.getpid()}.part")
        sha = hashlib.sha256()
        try:
            with urllib.request.urlopen(f"{base_url}/{name}", timeout=60) as response, partial.open("wb") as out:
                while chunk := response.read(1 << 20):
                    sha.update(chunk)
                    out.write(chunk)
            if sha.hexdigest() != digest:
                raise OSError(f"{name} does not match its pinned checksum")
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
    return directory


def _is_punctuation(char):
    """BERT's punctuation: every ASCII symbol, plus Unicode punctuation."""
    cp = ord(char)
    return 33 <= cp <= 47 or 58 <= cp <= 64 or 91 <= cp <= 96 or 123 <= cp <= 126 \
        or unicodedata.category(char).startswith("P")


def _is_cjk(cp):
    return (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0x20000 <= cp <= 0x2A6DF or 0x2A700 <= cp <= 0x2B73F
            or 0x2B740 <= cp <= 0x2B81F or 0x2B820 <= cp <= 0x2CEAF or 0xF900 <= cp <= 0xFAFF or 0x2F800 <= cp <= 0x2FA1F)


def _normalize(text):
    """BertNormalizer with lowercasing and accent stripping: what the model's tokenizer.json declares."""
    out = []
    for char in text:
        cp = ord(char)
        if cp == 0 or cp == 0xFFFD:
            continue
        if char in " \t\n\r" or unicodedata.category(char) == "Zs":
            out.append(" ")
        elif unicodedata.category(char) in ("Cc", "Cf"):
            continue
        elif _is_cjk(cp):
            out.append(f" {char} ")
        else:
            out.append(char)
    text = unicodedata.normalize("NFD", "".join(out))
    return "".join(char for char in text if unicodedata.category(char) != "Mn").lower()


def _words(text):
    """BertPreTokenizer: split on whitespace, with each punctuation character a word of its own."""
    for chunk in text.split():
        start = 0
        for index, char in enumerate(chunk):
            if _is_punctuation(char):
                if start < index:
                    yield chunk[start:index]
                yield char
                start = index + 1
        if start < len(chunk):
            yield chunk[start:]


class WordPiece:
    """Greedy longest-match WordPiece, read from a Hugging Face tokenizer.json."""

    def __init__(self, path):
        model = json.loads(Path(path).read_text())["model"]
        if model["type"] != "WordPiece":
            raise ValueError(f"unsupported tokenizer {model['type']}")
        self.vocab = model["vocab"]
        self.unk = self.vocab[model["unk_token"]]
        self.prefix = model["continuing_subword_prefix"]
        self.max_chars = model["max_input_chars_per_word"]
        self.cache = {}

    def word(self, word):
        """Ids of one pre-tokenized word; a word with no full split is one unknown token."""
        if (ids := self.cache.get(word)) is not None:
            return ids
        ids, start = [], 0
        while start < len(word) and len(word) <= self.max_chars:
            end = len(word)
            while end > start:
                piece = word[start:end] if start == 0 else self.prefix + word[start:end]
                if piece in self.vocab:
                    break
                end -= 1
            else:
                break
            ids.append(self.vocab[piece])
            start = end
        if start < len(word):
            ids = [self.unk]
        self.cache[word] = ids
        return ids

    def encode(self, text, limit=MAX_TOKENS):
        """At most `limit` token ids of `text`, without special tokens."""
        ids = []
        for word in _words(_normalize(text)):
            ids.extend(self.word(word))
            if len(ids) >= limit:
                return ids[:limit]
        return ids


def _safetensor(path, name):
    """One tensor of a safetensors file, memory-mapped rather than read."""
    with open(path, "rb") as handle:
        size = struct.unpack("<Q", handle.read(8))[0]
        header = json.loads(handle.read(size))
    entry = header[name]
    begin, end = entry["data_offsets"]
    dtype = np.dtype(DTYPES[entry["dtype"]])
    if (end - begin) != dtype.itemsize * int(np.prod(entry["shape"])):
        raise ValueError(f"{path}: {name} has an inconsistent size")
    return np.memmap(path, dtype=dtype, mode="r", offset=8 + size + begin, shape=tuple(entry["shape"]))


class StaticModel:
    """Mean of the token vectors of a text, unit length: model2vec's `encode` for an unweighted model."""

    def __init__(self, directory):
        directory = Path(directory)
        self.tokenizer = WordPiece(directory / "tokenizer.json")
        self.embedding = _safetensor(directory / "model.safetensors", "embeddings")
        tokens = sorted(self.tokenizer.vocab, key=self.tokenizer.vocab.get)
        # model2vec cuts characters past what MAX_TOKENS tokens of median length could cover.
        self.max_chars = MAX_TOKENS * int(np.median([len(token) for token in tokens]))

    @property
    def dim(self):
        return self.embedding.shape[1]

    def encode(self, texts):
        """A (len(texts), dim) float32 matrix of unit vectors; a text with no known token is all zeros."""
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        unk = self.tokenizer.unk
        for row, text in enumerate(texts):
            ids = [i for i in self.tokenizer.encode(text[:self.max_chars]) if i != unk]
            if ids:
                out[row] = self.embedding[ids].mean(axis=0)
        return out / (np.linalg.norm(out, axis=1, keepdims=True) + 1e-32)

