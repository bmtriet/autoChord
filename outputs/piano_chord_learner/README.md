# Piano Chord Learner

Prototype nay nghe MIDI controller cua ban, hoc cach ban bam hop am, roi phat hop am tu dong vao GarageBand khi ban chi choi melody.

## Cach noi voi GarageBand tren macOS

1. Cai thu vien:

   ```bash
   cd /Users/triet.bui/Documents/Codex/2026-06-16/t-b-m-h-p-m/outputs/piano_chord_learner
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Liet ke cong MIDI:

   ```bash
   python piano_chord_learner.py ports
   ```

3. Train: choi nhu binh thuong, tay trai bam hop am, tay phai choi melody. Mac dinh split point la C4/MIDI 60: note duoi 60 la hop am, note tu 60 tro len la melody.

   ```bash
   python piano_chord_learner.py train --input "TEN MIDI CONTROLLER" --model my_style.json
   ```

   Bam `Ctrl+C` de dung va luu model.

4. Play: tren may hien tai cua ban, script thay duoc input `SE49 MIDI1` va output `GarageBand Virtual In`, nen co the route thang vao GarageBand:

   ```bash
   python piano_chord_learner.py play --input "SE49 MIDI1" --output "GarageBand Virtual In" --model my_style.json --comp learned
   ```

   Bay gio ban choi melody bang tay phai; hop am se duoc phat tu dong theo cac mau ban da train. Neu model cu chua co pattern rai, script se tu fallback sang power-chord arpeggio: root -> fifth -> octave -> fifth.

   Neu may/DAW khong co `GarageBand Virtual In`, bo `--output ...`; script se tao mot cong MIDI ao ten `ChordLearner Out`.

## Meo train de nghe tu nhien hon

- Choi 5-10 phut cac vong hop am hay dung cua ban.
- Rai power chord dung cach ban hay choi, vi script moi luu lai ca thu tu note va timing tay trai trong khoang `--left-pattern-window`.
- Choi melody cung luc hoac ngay sau hop am; model se gan melody pitch class voi hop am gan nhat.
- Neu ban rai cham hon, tang cua so hoc pattern:

  ```bash
  python piano_chord_learner.py train --input "SE49 MIDI1" --model my_style.json --left-pattern-window 1.2 --verbose
  ```

- Neu ban hay choi hop am bang tay phai, doi split point:

  ```bash
  python piano_chord_learner.py train --input "TEN MIDI CONTROLLER" --split 55 --model my_style.json
  ```

## Lenh huu ich

```bash
python piano_chord_learner.py play --help
python piano_chord_learner.py train --help
```

## Debug khi hop am nhay loan xa

Ghi lai toan bo phim ban bam, khong phat hop am:

```bash
python piano_chord_learner.py record --input "SE49 MIDI1" --out session_notes.jsonl --split 60
```

Vua play vua log input note va moi quyet dinh auto-chord:

```bash
python piano_chord_learner.py play --input "SE49 MIDI1" --output "GarageBand Virtual In" --model my_style_v2.json --comp learned --event-log play_debug.jsonl --print-notes --verbose
```

Phan tich model va log:

```bash
python piano_chord_learner.py analyze --model my_style_v2.json --log play_debug.jsonl
```

Neu thay `Melody note ambiguity` bao mot note melody gan voi nhieu hop am, model se de nhay lung tung. Hay train lai cham hon, moi hop am rai thanh mot cum rieng, va de khoang cach nho giua cac hop am:

```bash
python piano_chord_learner.py train --input "SE49 MIDI1" --model my_style_v3.json --left-pattern-window 1.0 --new-chord-gap 0.55 --pair-window 1.4 --event-log train_v3_notes.jsonl --print-notes --verbose
```

## Mode progression, hop hon voi power chord rai

Neu melody-to-chord bi sai, dung progression mode. Mode nay doc chuoi hop am tay trai tu log train, roi moi lan melody trigger se phat hop am ke tiep trong progression. No khong doan hop am tu tung note melody nua.

```bash
python piano_chord_learner.py play --input "SE49 MIDI1" --output "GarageBand Virtual In" --model my_style_v4.json --mode progression --progression-trigger clock --progression-log train_v4_notes.jsonl --comp learned --event-log play_progression_debug.jsonl --verbose
```

Neu vao sai vi tri trong vong hop am:

```bash
python piano_chord_learner.py play --input "SE49 MIDI1" --output "GarageBand Virtual In" --model my_style_v4.json --mode progression --progression-trigger clock --progression-log train_v4_notes.jsonl --progression-start 3 --comp learned --verbose
```

Neu clock nhanh/cham hon toc do ban dang choi melody:

```bash
python piano_chord_learner.py play --input "SE49 MIDI1" --output "GarageBand Virtual In" --model my_style_v4.json --mode progression --progression-trigger clock --progression-log train_v4_notes.jsonl --progression-tempo 1.15 --comp learned --verbose
```

Hoac ep moi hop am dung mot thoi luong co dinh:

```bash
python piano_chord_learner.py play --input "SE49 MIDI1" --output "GarageBand Virtual In" --model my_style_v4.json --mode progression --progression-trigger clock --progression-log train_v4_notes.jsonl --progression-period 1.75 --comp learned --verbose
```

## Mode chart, hop nhat khi ban da co hop am tay

Neu da co chord chart nhu anh ban gui, bo hinh thuc doan melody va cho script di theo chart co dinh:

```bash
python piano_chord_learner.py play --input "SE49 MIDI1" --output "GarageBand Virtual In" --model my_style_v4.json --mode chart --chart-file chart_from_image.txt --progression-trigger clock --chart-period 1.75 --comp learned --verbose
```

Co san mot file mau tu anh:

```text
/Users/triet.bui/Documents/Codex/2026-06-16/t-b-m-h-p-m/outputs/piano_chord_learner/chart_from_image.txt
```

Neu muon tu dap sustain de sang hop am tiep theo:

```bash
python piano_chord_learner.py play --input "SE49 MIDI1" --output "GarageBand Virtual In" --model my_style_v4.json --mode chart --chart-file chart_from_image.txt --progression-trigger pedal --comp learned --verbose
```

## Mode lyric, de hop am phat theo loi bai hat

Mode nay can mot file timeline dang:

```text
time|chord|lyric
```

Vi du mau cho verse "Co gai den tu hom qua":

```text
/Users/triet.bui/Documents/Codex/2026-06-16/t-b-m-h-p-m/outputs/piano_chord_learner/co_gai_den_tu_hom_qua_verse_lyric_timeline.txt
```

Chay theo lyric timeline:

```bash
python piano_chord_learner.py play --input "SE49 MIDI1" --output "GarageBand Virtual In" --model my_style_v4.json --mode lyric --lyric-file co_gai_den_tu_hom_qua_verse_lyric_timeline.txt --progression-trigger clock --comp learned --verbose
```

Neu muon canh bang chan:

```bash
python piano_chord_learner.py play --input "SE49 MIDI1" --output "GarageBand Virtual In" --model my_style_v4.json --mode lyric --lyric-file co_gai_den_tu_hom_qua_verse_lyric_timeline.txt --progression-trigger pedal --comp learned --verbose
```

Khong co pedal, dung mot phim control rieng de sang hop am. Vi du `C#1` la MIDI note `25`:

```bash
python piano_chord_learner.py play --input "SE49 MIDI1" --output "GarageBand Virtual In" --model my_style_v4.json --mode lyric --lyric-file co_gai_den_tu_hom_qua_full_lyric_timeline.txt --progression-trigger control-note --control-note 25 --comp learned --verbose
```

Phim `C#1` se duoc script nuot lai, khong gui tieng cua phim do vao GarageBand.

## Import tu URL Hac

Lay toan bo lyric + chord tu mot URL HopAmChuan va tao 2 file:

- inline chord lyric
- lyric timeline uoc luong

```bash
python piano_chord_learner.py import-hopamchuan --url "https://hopamchuan.com/song/54/co-gai-den-tu-hom-qua/LNTguitar" --inline-out co_gai_den_tu_hom_qua_full_inline.txt --timeline-out co_gai_den_tu_hom_qua_full_lyric_timeline.txt --period 1.75
```

Nghe nhanh hon/cham hon:

```bash
python piano_chord_learner.py play --input "SE49 MIDI1" --output "GarageBand Virtual In" --model my_style.json --comp power --arp-step 0.11 --note-length 0.16 --retrigger 0.45
```

Neu muon nghe lai kieu block chord cu:

```bash
python piano_chord_learner.py play --input "SE49 MIDI1" --output "GarageBand Virtual In" --model my_style.json --comp block
```

## Gioi han cua prototype

- Ban dau no hoc theo melody note va hop am truoc do, chua phan tich style phuc tap nhu rhythm comping rieng tung bai.
- Neu GarageBand khong nhan virtual port, bat IAC Driver trong Audio MIDI Setup va chay voi `--output "IAC Driver Bus 1"`.
