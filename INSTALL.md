# Installing MeetRec, step by step

This is the long version of the Quickstart in [README.md](README.md). It assumes
no prior experience with the terminal, Python or Xcode. Every step says what you
are doing, the exact command to run, what you should see, and what to do when it
goes wrong.

Follow the steps in order. Later steps depend on earlier ones.

**Time and disk space.** About 20 minutes of your attention, plus downloads:
roughly 1.1 GB for the Python environment, 1.4 GB for the transcription model
and 31 MB for the speaker-separation models. Budget 4 GB of free disk space.

## How to run the commands in this guide

Open **Terminal**: press `Cmd + Space`, type `Terminal`, press Enter. A window
with a text prompt appears.

To run a command, copy the whole line, paste it into that window and press
Enter. The command has finished when the prompt comes back. Some commands print
nothing when they succeed — that is normal.

Keep the same Terminal window open for the whole guide.

---

## Step 1 — Check your macOS version

MeetRec needs macOS 13 (Ventura) or later, because it records system audio with
ScreenCaptureKit.

```bash
sw_vers -productVersion
```

You should see a version number such as `14.5` or `15.2`. If the first number is
13 or higher, continue.

**If it is lower than 13:** MeetRec cannot run on this Mac. Update macOS from
 → System Settings → General → Software Update, then come back.

## Step 2 — Install the Xcode Command Line Tools

MeetRec is written partly in Swift and is compiled on your machine. The compiler
(`swiftc`) and `git` both come from Apple's Command Line Tools.

First check whether you already have them:

```bash
swiftc --version
```

If that prints a version, skip to Step 3. If it prints `command not found`, or
opens a dialog offering to install the tools, run:

```bash
xcode-select --install
```

A macOS dialog appears. Click **Install**, accept the licence, and wait — it
downloads about 1 GB and can take several minutes. When it finishes, run
`swiftc --version` again to confirm.

**If you already have the full Xcode app** installed, the tools are included,
but `swiftc` can still fail if macOS is pointed at the wrong location. Fix it
with `sudo xcode-select --switch /Applications/Xcode.app` (it will ask for your
Mac password; nothing is echoed as you type).

## Step 3 — Download the project

This copies the project into a folder named `meetrec` inside your home
directory.

```bash
cd ~
```

```bash
git clone https://github.com/frasal-test/meetrec.git
```

```bash
cd meetrec
```

The first command moves to your home folder, the second downloads the project,
the third enters it. From now on every command in this guide assumes you are
inside that folder. If you close Terminal and come back later, run `cd ~/meetrec`
first.

Check that you are in the right place:

```bash
ls meetrec.sh
```

It should print `meetrec.sh`. If it prints `No such file or directory`, you are
in the wrong folder — run `cd ~/meetrec` and try again.

## Step 4 — Create the Python environment

MeetRec's transcription runs in Python. Rather than installing packages system
wide, it uses a *virtual environment*: a self-contained folder named `.venv`
inside the project. Nothing here touches the rest of your Mac.

```bash
python3 -m venv .venv
```

This prints nothing when it works.

```bash
.venv/bin/pip install -r requirements-diarization.txt
```

This one is slow — several minutes and about 1.1 GB, most of it PyTorch. You
will see a long stream of `Collecting…` and `Downloading…` lines. It is done
when the prompt returns and the last lines read `Successfully installed …`.

**Why `requirements-diarization.txt` and not `requirements.txt`?** Separating
remote speakers is switched on by default in MeetRec's configuration, and that
feature needs the extra packages. The smaller `requirements.txt` still produces
transcripts — they simply arrive without speaker separation, carrying a warning
that explains why. If that is what you want, see
[Running without speaker separation](#running-without-speaker-separation) at the
end of this guide and turn the feature off properly.

**If `python3` is not found:** it ships with the Command Line Tools, so redo
Step 2.

## Step 5 — Get a Hugging Face token

Speaker separation uses two models published by pyannote on Hugging Face. They
are free, but the authors require you to have an account and to accept their
conditions before downloading. You need a token to prove that.

Skip this step only if you decided to run without speaker separation.

1. Create a free account at <https://huggingface.co/join>, or sign in to the one
   you have.
2. While signed in, open both pages below and accept the conditions on each.
   Each shows a short form and a button such as *Agree and access repository*.
   You must be signed in, or you will only see a description and no form.
   - <https://huggingface.co/pyannote/segmentation-3.0>
   - <https://huggingface.co/pyannote/speaker-diarization-3.1>
3. Go to <https://huggingface.co/settings/tokens> and click **New token**. Give
   it any name, choose the **Read** type, and create it.
4. Copy the token. It starts with `hf_`. Hugging Face shows it once — copy it
   now.

Now save it into a file named `.env` in the project folder. Replace
`hf_your_token_here` with the token you copied, keeping the quotes off:

```bash
echo 'HF_TOKEN=hf_your_token_here' > .env
```

That file stays on your Mac and is never committed to git.

Check it was written:

```bash
cat .env
```

It should print one line, `HF_TOKEN=hf_…`, with your real token.

**The token must belong to the same account** that accepted the conditions in
point 2. This is the most common cause of a failure later on.

## Step 6 — Create a signing certificate

Do this once. Skipping it does not break MeetRec, but it makes it annoying:
macOS will ask for microphone and screen-recording permission again after every
rebuild, and the old permission entries pile up in System Settings, switched on
but no longer doing anything.

The reason is that macOS ties a permission grant to the exact identity of the
program. Without a certificate, the identity is a fingerprint of the compiled
file itself, which changes every time it is recompiled. With a certificate, the
identity stays stable across rebuilds.

The certificate is created in the Keychain Access app:

1. Open **Keychain Access** (`Cmd + Space`, type `Keychain Access`).
2. In the menu bar: **Keychain Access → Certificate Assistant → Create a
   Certificate…**
3. **Name:** type `MeetRec Dev` exactly — MeetRec looks for this name.
4. **Identity Type:** choose **Self Signed Root**.
5. **Certificate Type:** choose **Code Signing**.
6. Click **Create**, then **Done**.

Back in Terminal, confirm macOS considers it usable:

```bash
security find-identity -v -p codesigning
```

The output should include a line containing `"MeetRec Dev"`.

**If it does not appear:** find the certificate in Keychain Access, double-click
it, expand the **Trust** section, and set **Code Signing** to *Always Trust*.
Close the window (macOS asks for your password) and run the command again.

If you prefer a different certificate name, set `MEETREC_SIGN_IDENTITY` to that
name in your shell before building.

## Step 7 — Build and install

This compiles both programs, signs them with the certificate from Step 6,
installs a LaunchAgent so MeetRec starts automatically when you log in, and
launches it now.

```bash
./meetrec.sh install
```

Expect output along these lines:

```text
Built: /Users/you/meetrec/.recorder
Signed: /Users/you/meetrec/.recorder (com.frasal.meetrec.recorder) with 'MeetRec Dev'
Built: /Users/you/meetrec/MeetRec.app
Signed: /Users/you/meetrec/MeetRec.app (com.frasal.meetrec.menubar) with 'MeetRec Dev'
MeetRec is installed and running in the menu bar.
```

A ✒︎ icon appears in the macOS menu bar, at the top right of the screen.

**If the last lines say `Signed ad-hoc` with a warning**, the certificate from
Step 6 was not found. MeetRec still works; go back to Step 6 if you would rather
not re-grant permissions after each rebuild.

**If you see `permission denied`**, run `chmod +x meetrec.sh meet.sh` and try
again.

**If no icon appears**, see [Troubleshooting](#troubleshooting).

## Step 8 — Grant the macOS permissions

macOS does not let any app hear you or capture other apps' sound without an
explicit grant, and it only asks the first time the app actually tries.

Click the ✒︎ icon and choose **Start recording — Italiano** (or whichever
language you want). macOS shows one or two permission dialogs:

- **Microphone** — click **Allow**.
- **Screen & System Audio Recording** — this one usually opens System Settings
  instead of a simple dialog. Enable **MeetRec** in the list.

Then click ✒︎ → **Stop recording**. This first recording exists only to trigger
the prompts; you can delete it later.

After granting screen recording, quit and restart MeetRec so it picks the
permission up: ✒︎ → **Quit MeetRec**, then run `./meetrec.sh install` again.

**If no dialog appeared**, add MeetRec by hand:  → System Settings → Privacy
& Security → **Microphone**, and again under **Screen & System Audio
Recording**. If MeetRec is listed but switched off, switch it on. If it is not
listed at all, the app never tried to record — check the logs described in
[Troubleshooting](#troubleshooting).

## Step 9 — Verify the installation

```bash
./meetrec.sh doctor
```

Every line should start with `✓`:

```text
✓ macOS 13+: 15.2
✓ Swift compiler: /usr/bin/swiftc
✓ Recorder binary: /Users/you/meetrec/.recorder
✓ Menu bar binary: /Users/you/meetrec/MeetRec.app/Contents/MacOS/MeetRec
✓ faster-whisper: Python module
✓ pyannote.audio: required by diarize_system=true
✓ Hugging Face token: HF_TOKEN or HUGGINGFACE_TOKEN
✓ Recordings directory: /Users/you/meetrec/recordings
```

Any `✗` points at the step that did not take: `faster-whisper` or
`pyannote.audio` at Step 4, `Hugging Face token` at Step 5, the two binaries at
Step 7.

Below the list, `doctor` prints one more line, something like:

```text
Permissions as seen by this terminal: screen_audio=missing microphone=authorized identity=com.frasal.meetrec.menubar
```

**This describes Terminal, not MeetRec** — `screen_audio=missing` here does not
mean the app is missing the permission. macOS attributes a permission check to
whichever process asks, so run from Terminal it cannot see the app's own grants.
Ignore this line and trust System Settings instead; the note printed underneath
it says the same thing. It is also excluded from the pass/fail result on
purpose.

## Step 10 — Record your first meeting

**Wear headphones.** Without them your microphone picks up the other
participants through your speakers, and they end up transcribed twice — once
from each track.

1. Click ✒︎ and choose the language of the meeting: **Italiano**, **English**,
   **Español**, or **Auto detect** if you are not sure.
2. The icon changes to show that recording is running.
3. At the end, click ✒︎ → **Stop recording**.

Transcription then starts in the background — you can start another meeting
while it runs. The menu-bar icon shows the progress percentage, and the menu
holds a progress bar naming the current phase. You get a macOS notification when
it is done.

**The first transcription is much slower than the following ones**, because it
downloads the `medium` model (about 1.4 GB) into `~/.cache/huggingface` before
transcribing anything. Later recordings reuse it. Speaker separation adds
another 31 MB on its first run.

### Where your files are

Click ✒︎ → **Open recordings**, or look in `~/meetrec/recordings/`. Each session
is a folder named after its start time, for example
`2026-07-29T10-30-00/`, containing:

- `transcripts/transcript.md` — the readable transcript; start here.
- `transcripts/transcript.txt`, `.srt`, `.json` — plain text, subtitles, and the
  full structured output with timings and speakers.
- `transcripts/transcript.speakers.txt` — grouped by speaker.
- `audio/mic.caf` and `audio/system.caf` — your voice and everyone else's, kept
  as separate recordings.
- `transcribe.log` — what happened during transcription. Read this when
  something failed.

Your voice is always labelled `ME`. Remote participants are separated on the
system track when speaker separation is enabled.

---

## Troubleshooting

### No ✒︎ icon in the menu bar

Check whether the background agent is running:

```bash
launchctl print "gui/$(id -u)/com.frasal.meetrec" | head -5
```

If it reports that the service could not be found, run `./meetrec.sh install`
again. If it is running but invisible, look at the error log:

```bash
cat .menubar.stderr.log
```

A menu bar crowded with icons can also simply hide it — try quitting another
menu-bar app.

### "Python environment missing" alert when starting a recording

The `.venv` folder is missing or incomplete. Redo Step 4. This also happens if
you moved or renamed the project folder after installing: run
`./meetrec.sh install` again from the new location.

### `Missing Python environment` when running ./meet.sh

Same cause as above — redo Step 4.

### doctor shows ✗ pyannote.audio

You installed `requirements.txt` instead of `requirements-diarization.txt`.
Either install the full set:

```bash
.venv/bin/pip install -r requirements-diarization.txt
```

or turn speaker separation off, as described in
[Running without speaker separation](#running-without-speaker-separation).

Left as is you still get transcripts, but every remote participant is labelled
`REMOTE` instead of being told apart, and each transcript opens with a line
saying so. To get the speakers separated on a meeting already transcribed this
way, install the packages and then
[re-transcribe the session](#re-transcribing-a-session).

### doctor shows ✗ Hugging Face token

The `.env` file is missing, is in the wrong folder, or has a typo. It must sit in
the project root, next to `meetrec.sh`, and contain a line `HF_TOKEN=hf_…`.
Check with `cat .env` from inside `~/meetrec`.

### "Cannot access the pyannote diarization model on Hugging Face"

The token is valid but cannot reach the models. Almost always one of:

- The conditions in Step 5, point 2 were never accepted — do it for **both**
  pages.
- The token belongs to a different Hugging Face account than the one that
  accepted them.
- The token was created as *write* or *fine-grained* without read access to
  gated repositories. Create a plain **Read** token instead.

The meeting is still transcribed while you sort this out — without speaker
separation, and with a warning at the top of the transcript. Once the token
works, [re-transcribe the session](#re-transcribing-a-session) to get the
speakers apart.

### The transcript starts with a line beginning "[MeetRec]"

That line is MeetRec telling you the transcript was produced with something
switched off — almost always speaker separation, because pyannote is missing or
the Hugging Face token was refused. The transcript itself is complete and
usable; only the labelling of remote participants is affected, and they all
appear as `REMOTE`.

The same text is in `transcripts/transcript.md` as a quoted warning and in
`transcripts/transcript.json` under `warnings`. Fix the cause it names, then
[re-transcribe the session](#re-transcribing-a-session) if you want that meeting
redone.

### Re-transcribing a session

Transcribing again is safe: it overwrites the files in `transcripts/` and never
touches the recorded audio in `audio/`.

A finished session is not picked up again unless you force it, which is what
`--force` is for. Replace `recordings/2026-07-29T10-30-00` with the folder of
the meeting you want:

```bash
.venv/bin/python -m meeting_recorder.control process recordings/2026-07-29T10-30-00 --force
```

This transcribes while you wait, printing progress. Leave it running until the
prompt returns.

### macOS asks for permissions again after every rebuild

The programs are ad-hoc signed — Step 6 was skipped or the certificate is not
trusted. Create the certificate, then clear the stale grants:

```bash
tccutil reset ScreenCapture com.frasal.meetrec.menubar
```

```bash
tccutil reset Microphone com.frasal.meetrec.menubar
```

Then run `./meetrec.sh install` and grant the prompts one last time.

### "Recording failed or produced no audio tracks"

Neither track captured anything, which means the permissions from Step 8 are not
in place. Check both **Microphone** and **Screen & System Audio Recording** in
System Settings → Privacy & Security, and restart MeetRec after changing either.

### Everything is transcribed twice

You recorded without headphones, so your microphone captured the other
participants coming out of your speakers. Both tracks then contain the same
speech. Use headphones.

### Transcription seems stuck

Look at the session's log:

```bash
tail -f recordings/*/transcribe.log
```

During the first run you will see the model download. Press `Ctrl + C` to stop
watching the log — this does not stop the transcription.

### The transcript is full of repeated filler like "Okay."

A microphone recorded very quietly can make the model invent speech during
silence. MeetRec levels and filters the audio to prevent this, and the settings
are on by default. If it still happens, see *Speech leveling and filtering* in
[README.md](README.md).

---

## Running without speaker separation

Speaker separation is what distinguishes individual remote participants. Without
it, you still get a full transcript with your own voice labelled `ME` and
everyone else on the system track — you just do not learn which remote voice is
which. In exchange you skip Step 5 entirely, save about 1 GB of downloads, and
transcribe faster.

To turn it off, create the configuration file:

```bash
mkdir -p ~/.config/meetrec
```

```bash
cp config.example.json ~/.config/meetrec/config.json
```

Open `~/.config/meetrec/config.json` in any text editor and change
`"diarize_system": true` to `"diarize_system": false`. All the other settings
are explained in [README.md](README.md).

## Updating MeetRec

```bash
cd ~/meetrec
```

```bash
git pull
```

```bash
./meetrec.sh install
```

`install` recompiles only what actually changed, so permissions are left alone
when the code did not move.

## Uninstalling

Stop MeetRec and remove it from login:

```bash
./meetrec.sh uninstall
```

This keeps your recordings. To remove everything, delete the project folder
(`rm -rf ~/meetrec`) and, if you want the disk space back, the downloaded models
in `~/.cache/huggingface`.
