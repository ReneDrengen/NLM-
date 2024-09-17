# Copyright (c) Kyutai, all rights reserved.
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

import argparse
import asyncio
from dataclasses import dataclass
import random
import os
from pathlib import Path
import tarfile
import time
import secrets
import sys

import aiohttp
from aiohttp import web
from huggingface_hub import hf_hub_download
import numpy as np
import sentencepiece
import sphn
import torch


from .client_utils import make_log
from .models import loaders, MimiModel, LMModel, LMGen


def log(level: str, msg: str):
    print(make_log(level, msg))


def seed_all(seed):
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)  # for multi-GPU setups
    random.seed(seed)
    np.random.seed(seed)
    torch.backends.cudnn.deterministic = False
    torch.backends.cudnn.benchmark = False


@dataclass
class ServerState:
    mimi: MimiModel
    text_tokenizer: sentencepiece.SentencePieceProcessor
    lm_gen: LMGen
    lock: asyncio.Lock

    def __init__(self, mimi: MimiModel, text_tokenizer: sentencepiece.SentencePieceProcessor,
                 lm: LMModel, device: str | torch.device):
        self.mimi = mimi
        self.text_tokenizer = text_tokenizer
        self.lm_gen = LMGen(lm)

        self.device = device
        self.frame_size = int(self.mimi.sample_rate / self.mimi.frame_rate)
        self.lock = asyncio.Lock()

        self.mimi.streaming_forever(1)
        self.lm_gen.streaming_forever(1)

    def warmup(self):
        for chunk in range(4):
            chunk = torch.zeros(1, 1, self.frame_size, dtype=torch.float32, device=self.device)
            codes = self.mimi.encode(chunk)
            for c in range(codes.shape[-1]):
                tokens = self.lm_gen.step(codes[:, :, c: c + 1])
                if tokens is None:
                    continue
                _ = self.mimi.decode(tokens[:, 1:])
        torch.cuda.synchronize()

    async def handle_chat(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        async def recv_loop():
            nonlocal close
            try:
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.ERROR:
                        log("error", f"{ws.exception()}")
                        break
                    elif message.type == aiohttp.WSMsgType.CLOSED:
                        break
                    elif message.type != aiohttp.WSMsgType.BINARY:
                        log("error", f"unexpected message type {message.type}")
                        continue
                    message = message.data
                    if not isinstance(message, bytes):
                        log("error", f"unsupported message type {type(message)}")
                        continue
                    if len(message) == 0:
                        log("warning", "empty message")
                        continue
                    kind = message[0]
                    if kind == 1:  # audio
                        payload = message[1:]
                        opus_reader.append_bytes(payload)
                    else:
                        log("warning", f"unknown message kind {kind}")
            finally:
                close = True
                log("info", "connection closed")

        async def opus_loop():
            all_pcm_data = None

            while True:
                if close:
                    return
                await asyncio.sleep(0.001)
                pcm = opus_reader.read_pcm()
                if pcm.shape[-1] == 0:
                    continue
                if all_pcm_data is None:
                    all_pcm_data = pcm
                else:
                    all_pcm_data = np.concatenate((all_pcm_data, pcm))
                while all_pcm_data.shape[-1] >= self.frame_size:
                    be = time.time()
                    chunk = all_pcm_data[: self.frame_size]
                    all_pcm_data = all_pcm_data[self.frame_size:]
                    chunk = torch.from_numpy(chunk)
                    chunk = chunk.to(device=self.device)[None, None]
                    codes = self.mimi.encode(chunk)
                    for c in range(codes.shape[-1]):
                        tokens = self.lm_gen.step(codes[:, :, c: c + 1])
                        if tokens is None:
                            continue
                        assert tokens.shape[1] == self.lm_gen.lm_model.dep_q + 1
                        main_pcm = self.mimi.decode(tokens[:, 1:])
                        main_pcm = main_pcm.cpu()
                        opus_writer.append_pcm(main_pcm[0, 0].numpy())
                        text_token = tokens[0, 0, 0].item()
                        if text_token not in (0, 3):
                            _text = self.text_tokenizer.id_to_piece(text_token)  # type: ignore
                            _text = _text.replace("▁", " ")
                            msg = b"\x02" + bytes(_text, encoding="utf8")
                            log("info", f"text token '{_text}'")
                            await ws.send_bytes(msg)
                    log("info", f"frame handled in {1000 * (time.time() - be):.1f}ms")

        async def send_loop():
            while True:
                if close:
                    return
                await asyncio.sleep(0.001)
                msg = opus_writer.read_bytes()
                if len(msg) > 0:
                    await ws.send_bytes(b"\x01" + msg)

        log("info", "accepted connection")
        close = False
        async with self.lock:
            opus_writer = sphn.OpusStreamWriter(self.mimi.sample_rate)
            opus_reader = sphn.OpusStreamReader(self.mimi.sample_rate)
            self.mimi.reset_streaming()
            self.lm_gen.reset_streaming()
            # Send the handshake.
            await ws.send_bytes(b"\x00")
            await asyncio.gather(opus_loop(), recv_loop(), send_loop())
        log("info", "done with connection")
        return ws


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost", type=str)
    parser.add_argument("--port", default=8998, type=int)
    parser.add_argument("--static", type=str)
    parser.add_argument("--gradio_tunnel", action='store_true', help='Activate a gradio tunnel.')
    parser.add_argument("--gradio_tunnel_token",
                        help='Provide a custom (secret) token here to keep getting the same URL.')

    parser.add_argument("--tokenizer-name", type=str, default="tokenizer_spm_32k_3.model",
                        help="Name of the text tokenizer file in the given HF repo, or path to a local file.")
    parser.add_argument("--moshi-name", type=str, default="moshiko",
                        help="Name of the Moshi checkpoint in the given HF repo, or path to a local file.")
    parser.add_argument("--mimi-name", type=str, default="mimi",
                        help="Name of the Mimi checkpoint in the given HF repo, or path to a local file.")
    parser.add_argument("--hf-repo", type=str, default=loaders.HF_REPO,
                        help="HF repo to look into, defaults to Kyutai official one.")
    parser.add_argument("--device", type=str, default="cuda", help="Device on which to run, defaults to 'cuda'.")

    args = parser.parse_args()
    seed_all(42424242)

    setup_tunnel = None
    tunnel_token = ''
    if args.gradio:
        try:
            from gradio import networking  # type: ignore
        except ImportError:
            log("error", "Cannot find gradio which is required to activate a tunnel. "
                         "Please install with `pip install gradio`.")
            sys.exit(1)
        setup_tunnel = networking.setup_tunnel
        if args.gradio_tunnel_secret is None:
            tunnel_token = secrets.token_urlsafe(32)
        else:
            tunnel_token = args.gradio_tunnel_secret

    log("info", "loading mimi")
    mimi_path = loaders.resolve_model_checkpoint(args.mimi_name, args.hf_repo, allow_local_file=True)
    mimi = loaders.get_mimi(mimi_path, args.device)
    log("info", "mimi loaded")

    tokenizer_path = loaders.resolve_model_checkpoint(args.tokenizer_name, args.hf_repo, allow_local_file=True)
    text_tokenizer = sentencepiece.SentencePieceProcessor(tokenizer_path)  # type: ignore
    log("info", "loading moshi")

    moshi_path = loaders.resolve_model_checkpoint(args.moshi_name, args.hf_repo, allow_local_file=True)
    lm = loaders.get_moshi_lm(moshi_path, args.device)

    state = ServerState(mimi, text_tokenizer, lm, args.device)
    log("info", "warming up the model")
    state.warmup()
    app = web.Application()
    app.router.add_get("/api/chat", state.handle_chat)
    static_path: None | str = None
    if args.static is None:
        log("info", "retrieving the static content")
        dist_tgz = hf_hub_download(args.hf_repo, "dist.tgz")
        dist_tgz = Path(dist_tgz)
        dist = dist_tgz.parent / "dist"
        if not dist.exists():
            with tarfile.open(dist_tgz, "r:gz") as tar:
                tar.extractall(path=dist_tgz.parent)
        static_path = str(dist)
    elif args.static != "none":
        # When set to the "none" string, we don't serve any static content.
        static_path = args.static
    if static_path is not None:
        async def handle_root(_):
            return web.FileResponse(os.path.join(static_path, "index.html"))

        log("info", f"serving static content from {static_path}")
        app.router.add_get("/", handle_root)
        app.router.add_static(
            "/", path=static_path, follow_symlinks=True, name="static"
        )
    if setup_tunnel is not None:
        setup_tunnel('localhost', args.port, tunnel_token, None)
    log("info", f"listening to ws://{args.host}:{args.port}")
    web.run_app(app, port=args.port)


with torch.no_grad():
    main()
