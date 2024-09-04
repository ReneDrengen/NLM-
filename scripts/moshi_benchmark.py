# Copyright (c) Kyutai, all rights reserved.
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

import argparse
import msh
import sentencepiece
import torch
import torchaudio
import numpy as np
import random
import time

from torch.profiler import profile, ProfilerActivity

SAMPLE_RATE = msh.models.moshi.SAMPLE_RATE
DEVICE = "cuda:0"
ENABLE_PROFILING = False

parser = argparse.ArgumentParser()
parser.add_argument("--tokenizer", type=str)
parser.add_argument("--moshi-weights", type=str)
parser.add_argument("--mimi-weights", type=str)
parser.add_argument("--steps", default=100, type=int)
parser.add_argument("--profile", action="store_true")
args = parser.parse_args()


def seed_all(seed):
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)  # for multi-GPU setups
    random.seed(seed)
    np.random.seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


seed_all(42424242)


print("loading mimi")
ec = msh.models.moshi.get_encodec(args.mimi_weights, DEVICE)
print("mimi loaded")
text_tokenizer = sentencepiece.SentencePieceProcessor(args.tokenizer)

print("loading moshi")
lm = msh.models.moshi.get_lm(args.moshi_weights, DEVICE)
lm.to(torch.bfloat16)
print("lm loaded")

lm_gen = msh.models.LMGen(lm)


def cb(step, total):
    print(f"{step:06d} / {total:06d}", end="\r")


def streaming_test(bs):

    main_audio = []
    main_text = []

    def run_step():
        start_time = time.time()
        # Chunk should contain the pcm data from the user, single channel with a sample rate of 24000.
        chunk = torch.zeros((bs, 1, 1920), dtype=torch.float, device=DEVICE)
        codes = ec.encode(chunk)
        assert codes.shape[-1] == 1
        for c in range(codes.shape[-1]):
            be = time.time()
            ev = torch.cuda.Event(enable_timing=True)
            ev.record()
            tokens = lm_gen.step(codes[:, :, c: c + 1])
            if tokens is None:
                print("Skipping")
                return
            evb = torch.cuda.Event(enable_timing=True)
            evb.record()
            dt_step = time.time() - be
            # if all([t < 2048 for t in tokens[1:]]):
            text_tokens = tokens[:, 0, 0]
            audio_tokens = tokens[:, 1:, :]
            # assert tokens.amax() < 2048, tokens
            # assert audio_tokens.max() < 2048, audio_tokens
            main_pcm = ec.decode(audio_tokens)
            # main_pcm is the audio to be played back to the user, here we just append it and store it in
            # a file once the loop is finished.
            main_audio.append(main_pcm[0])
        evb.synchronize()
        dg = ev.elapsed_time(evb)
        torch.cuda.synchronize()
        dt = time.time() - start_time
        print(f"step time: {1000 * dt:.2f}ms, lm step: {1000 * dt_step:.2f}, gpu step {dg:.2f}")
        text_token = text_tokens[0].item()
        if text_token not in (0, 3):
            _text = text_tokenizer.id_to_piece(text_token)
            _text = _text.replace("▁", " ")
            main_text.append(_text)

    for step in range(args.steps):
        run_step()
    print()
    if args.profile:
        with profile(
            activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA], with_stack=True
        ) as prof:
            for step in range(5):
                run_step()
        print()
        prof.export_chrome_trace("trace.json")
    main_audio = torch.cat(main_audio, dim=-1)
    print(main_audio.shape)
    print("generated text:")
    print("".join(main_text))
    torchaudio.save("gen_main.wav", main_audio.cpu(), SAMPLE_RATE)


print("streaming test")
bs = 1
with torch.no_grad():
    with ec.streaming(bs), lm_gen.streaming(bs):
        streaming_test(bs)
