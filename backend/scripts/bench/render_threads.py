"""How the render pool's size (APP_RENDER_THREADS) changes a burst's
throughput and the event loop's lag, in one process.

    python -m scripts.bench.render_threads --renders 120 --threads 1 2 3 4

A burst of documents is handed to a pool of T threads, as
app/core/layout/pool.py does, while the event loop runs a probe that asks to
wake every 5 ms -- a request's await, in miniature. Per T: the burst's wall
time, renders a second, and how late the loop woke (p50/p95/p99/max, ms).

The burst mixes what a send draws: a ticket (logo and icon), a certificate
over a 2000 x 1100 artwork, another ticket, and a ticket's PNG (pdfium,
behind its process-wide lock). Pure renders: no database, no bucket, and
not wkhtmltopdf (HTML designs and receipts), which runs as a process of its
own, outside the GIL, and costs memory rather than the loop's time.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from scripts.bench import _env  # noqa: F401  (settings before app)

VALUES = {
    "attendee_name": "Ananya Iyer", "first_name": "Ananya", "event_name": "Builders Day", "event_date": "16 Sept 2026",
    "event_time": "09:00", "venue": "KTPO", "org_name": "Yukthi", "ticket_type": "VIP", "ticket_code": "MOA-7K3QF-X9PWM",
    "certificate_no": "CERT-7K3QF-X9PWM", "issued_date": "18 Sept 2026", "role": "Attendee",
}
COLORS = {"primary": "#1db9a0", "secondary": "#aaaaaa", "ticketType": "#ff5a1f"}


def _documents():
    from PIL import Image

    from app.core.layout.renderer import RenderInput, pdf_to_png, render_pdf
    from app.core.layout.starters import certificate_starter, ticket_starter

    def png(w, h, color):
        b = io.BytesIO()
        Image.new("RGB", (w, h), color).save(b, format="PNG")
        return b.getvalue()

    def jpg(w, h):
        b = io.BytesIO()
        Image.effect_noise((w, h), 24).convert("RGB").save(b, format="JPEG", quality=40)
        return b.getvalue()

    logo, icon, art = png(512, 512, (20, 120, 200)), png(800, 800, (200, 80, 40)), jpg(2000, 1100)
    t, c = ticket_starter("classic"), certificate_starter("classic")

    def ticket():
        return render_pdf(RenderInput(title="t", canvas=t["canvas"], layers=t["layers"], page=t["page"], values=VALUES,
                                      qr_value="MOA-7K3QF-X9PWM", colors=COLORS, org_logo=logo, event_icon=icon))

    def certificate_with_art():
        i = RenderInput(title="c", canvas=c["canvas"], layers=c["layers"], page=c["page"], values=VALUES,
                        qr_value="certificate:MOA-7K3QF-X9PWM", colors=COLORS, org_logo=logo, event_icon=icon)
        i.template = art
        return render_pdf(i)

    pdf = ticket()

    def ticket_png():
        return pdf_to_png(pdf, width_px=1000)

    return ticket, certificate_with_art, ticket_png


async def burst(mix, renders: int, threads: int) -> dict:
    pool = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="render")
    loop = asyncio.get_running_loop()
    for _ in range(threads * 2):  # every thread warm
        await loop.run_in_executor(pool, mix[0])
    lags: list[float] = []
    done = asyncio.Event()

    async def probe():
        while not done.is_set():
            t0 = time.perf_counter()
            await asyncio.sleep(0.005)
            lags.append((time.perf_counter() - t0 - 0.005) * 1000)

    task = asyncio.create_task(probe())
    t0 = time.perf_counter()
    await asyncio.gather(*(loop.run_in_executor(pool, mix[i % len(mix)]) for i in range(renders)))
    wall = time.perf_counter() - t0
    done.set()
    await task
    pool.shutdown()
    lags.sort()

    def q(f: float) -> float:
        return round(lags[min(len(lags) - 1, int(len(lags) * f))], 1)

    return dict(threads=threads, wall_s=round(wall, 2), per_s=round(renders / wall, 1),
                lag_p50=q(0.5), lag_p95=q(0.95), lag_p99=q(0.99), lag_max=round(lags[-1], 1))


async def main(renders: int, sizes: list[int]) -> None:
    ticket, certificate_with_art, ticket_png = _documents()
    for fn in (ticket, certificate_with_art, ticket_png):
        xs = []
        for _ in range(10):
            t0 = time.perf_counter()
            fn()
            xs.append((time.perf_counter() - t0) * 1000)
        print(f"{fn.__name__:<22} alone: p50 {statistics.median(xs):6.1f} ms")
    mix = [ticket, certificate_with_art, ticket, ticket_png]
    print(f"a burst of {renders}; the loop asks to wake every 5 ms; lag in ms")
    for t in sizes:
        print("  ".join(f"{k}={v}" for k, v in (await burst(mix, renders, t)).items()), flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--renders", type=int, default=120)
    ap.add_argument("--threads", type=int, nargs="+", default=[1, 2, 3, 4])
    args = ap.parse_args()
    asyncio.run(main(args.renders, args.threads))
