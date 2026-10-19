# MT5 Basket Bot — dashboard autotrade lokal

Dashboard web localhost (port 8895) untuk kontrol bot autotrading di MT5:
follow-trend entry, **martingale layering tanpa SL/TP per posisi**, close basket
saat total profit kena target `$`, jam kerja per hari, start/stop dari web.

## Cara jalan

1. Buka **MT5 terminal** (login akun demo/real yang mau dipakai), pastikan tombol **Algo Trading** ON.
2. Double-click `start.bat` → dashboard terbuka di `http://127.0.0.1:8895/`.
3. Klik **⟳ Muat daftar pair** (sekalian ngecek koneksi MT5).
4. Pilih pair + parameter, klik **▶ START BOT**. **■ STOP** di web = bot berhenti ambil entry baru.
5. **☠ PANIC close all** = tutup SEMUA posisi basket + bot stop (wajib ketik `PANIK`).

## Alur strategy

1. **Tren** dari EMA50/EMA200 (timeframe pilihan): harga > EMA50 > EMA200 = UP → BUY; sebaliknya = DOWN → SELL; menyerong = tunggu.
2. **Layer 1** masuk kalau basket kosong & tren jelas & dalam jam kerja.
3. **Layering**: harga melawan posisi basket sejauh `step` → tambah layer, lot = `lot awal × multiplier^n`.
4. **Close basket**: total profit semua layer ≥ `TP basket $` (default $10) → close SEMUA posisi. Tanpa SL, tanpa TP per posisi.
5. **Di luar jam kerja**: gak ada entry baru, tapi basket tetap dikelola (TP/kill tetap jalan).

## Guard (wajib, gak bisa dihapus dari kode)

| Guard | Default |
|---|---|
| Max layer cap | 6 |
| Kill-switch: floating ≤ `-$` batas → **close all + STOP otomatis** | $150 |

Posisi bot ditandai magic `990001` — order manual lu di MT5 gak disentuh bot.

## Mode DEMO / LIVE

- Default mode: **DEMO**. Ganti ke LIVE wajib **ketik "LIVE"** di konfirmasi (2 langkah).
- Mode dicek saat START: mode=DEMO + akun REAL terlogin → ditolak; mode=LIVE + akun DEMO → ditolak.
- Perhatian: instruksi wajib: **mode dipilih harus cocok sama akun yang terbuka di terminal MT5** (mode = niat guard, akun = kenyataan; server memastikan dua-duanya sama sebelum jalan).

## Catatan

- Bot state disimpan di `state.json` (config + riwayat basket). Restart server = bot selalu mulai STOP.
- Kalau server mati, loop strategy mati — posisi yang udah kebuka TETAP di MT5 (bukan hilang).
- Dilarang pakai live tanpa paham resiko martingale: profit $ konsisten, tapi kena tren lawan panjang = floating minus numpuk cepat; kill-switch itu rem paling keras di sini.
- Dependency: Python 3.10+ + paket `MetaTrader5` (sudah ada). Server stdlib pure (tanpa pip tambahan).
