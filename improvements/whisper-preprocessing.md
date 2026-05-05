# Whisper Preprocessing Improvements

> **Status:** recommendations #1–#7 and #11 are implemented (high-impact tier shipped, plus the engine-level VAD gate). #8 (pre-emphasis), #9 (stereo channel guard), #10 (VAD-aware partial trim) remain deferred per the doc's own "measure first" guidance. The "What Korder does today" snapshot below describes the *pre*-implementation baseline and is preserved for reference.

## What Korder does today

Confirmed by reading the code: Korder does **no DSP preprocessing** between PortAudio and Whisper beyond a static gain multiplier. `MicRecorder._callback` in `src/korder/audio/capture.py:53-67` extracts channel 0 of the PortAudio float32 buffer, multiplies by `self.gain` (default 0.7 from `src/korder/config.py:11`) and fans the chunk out to subscribers. The dictation collector subscriber (`_collect_dictation`) just appends each chunk to a list; `stop()` returns the concatenated buffer to the UI. The UI passes that buffer (or a `webrtcvad`-trimmed segment, see `src/korder/ui/main_window.py:629-668`) straight to `WhisperEngine.transcribe`, whose only gate is a coarse `_has_speech_energy` RMS check at 0.005 (`src/korder/transcribe/whisper_engine.py:66-70`); audio is then handed to `pywhispercpp` unchanged. There is no DC removal, no high-pass filter, no pre-emphasis, no AGC, no silence trim, no resampling-quality control, and `initial_prompt` defaults to empty string in `config.py:29`. The only “quality” lever currently exercised is `VolumeDucker` (separate concern: speaker bleed, not captured-audio shaping) and webrtcvad-driven *segmentation* (where to cut), not signal *cleanup*.

For reference, Whisper itself (whisper.cpp internal, `whisper_pcm_to_mel`) resamples to 16 kHz if needed, applies a Hann-windowed STFT (n_fft=400, hop=160), computes 80-bin log-mel, and clamps + scales to [-1, 1] before the encoder. It does **not** apply DC removal, HPF, pre-emphasis, gating, or AGC. Whatever junk is in our buffer reaches the encoder via the mel filterbank.

## What openwakeword does (reference)

Source: `/home/szymon/priv/korder/.venv/lib/python3.12/site-packages/openwakeword/`.

The pipeline `predict(x)` (`model.py:232-275`) runs is shorter than one might expect:

1. **Optional Speex noise suppression** (`model.py:481-504`, off unless `enable_speex_noise_suppression=True`). Frame size 160 samples (10 ms @ 16 kHz). Korder’s wake path does **not** enable it (`src/korder/audio/wake.py:89-92`). Speex NS is a frequency-domain Wiener filter that estimates the noise PSD from non-speech frames; it’s only useful in clearly noisy environments and tends to smear transients on clean audio.
2. **Type cast** in `AudioFeatures._streaming_features` (`utils.py:409-452`). Audio must already be int16 PCM 16 kHz. The wake path converts in `wake.py:126`: `(np.clip(chunk, -1.0, 1.0) * 32767.0).astype(np.int16)`.
3. **Buffering to 80 ms (1280 samples) chunks** before invoking the melspec model.
4. **Mel-spectrogram via Google `speech_embedding` ONNX** (`utils.py:180-208`). Parameters baked into the ONNX graph: window 25 ms (400 samples), hop 10 ms (160 samples), 32 mel bins. Output: 76 frames × 32 mels per 1280-sample chunk (the `[1, 76, 32, 1]` tensor shape in `utils.py:149` confirms this).
5. **A single arithmetic transform on the mel: `x/10 + 2`** (`utils.py:180`, `melspec_transform=lambda x: x/10 + 2`). This is a constant scale + bias to align ONNX numerics with the upstream TF model — *not* a normalization based on the input signal.
6. **Embedding model** (`speech_embedding`) projects each 76×32 mel window to a 96-dim feature vector; downstream wake-word models classify on those.

What openwakeword **does not** do, also worth stating clearly because it’s the relevant precedent:

- No DC removal on the input.
- No high-pass filter.
- No pre-emphasis.
- No RMS normalization / AGC.
- No silence trimming.
- No mean-variance normalization at the mel level.

The `x/10 + 2` constants are a *model-vendor-numerical* hack, not a signal-conditioning step; they aren’t portable to Whisper or anywhere else.

So openwakeword is informative *as a baseline that says “raw int16 PCM at 16 kHz works fine for an ONNX feature extractor”* — but it doesn’t give Korder a recipe to copy. Whisper is more sensitive than a wake-word classifier (it hallucinates on silence, follows low-frequency junk into the encoder), and a few classical ASR-frontend steps absent from openwakeword can help here. Note also: openwakeword’s authors *do* expose Speex NS as an option even though they default it off — implying that for noisier clients, denoising is the lever they reach for.

## Recommendations

### High-impact, low-risk (ship these)

#### ✅ ~~1. DC offset removal per chunk~~

*Implemented.* `src/korder/audio/dsp.py:remove_dc`, called from `capture.py:_callback` before HPF + limiter.

- **Where**: `src/korder/audio/capture.py:_callback` — apply right where `chunk *= self.gain` already lives.
- **What**: `chunk -= chunk.mean()` on each PortAudio frame (typical 1024–4096 samples).
- **Why**: Some USB / Bluetooth / PipeWire-virtual mics emit a small but persistent DC bias. Whisper’s mel filterbank starts at 0 Hz, so a DC offset shows up as low-frequency energy that occupies the bottom mel bin and slightly distorts log-mel statistics the encoder expects to be roughly zero-mean. Cost is one numpy mean + one in-place subtract on a small chunk: well under 50 µs for 16 kHz × 10–100 ms chunks. Risk on a clean signal: zero — true zero-mean speech is unaffected.
- **Trade-off**: Must be applied *per chunk*, not once over the whole utterance — applying once at the end means low-frequency noise has already been gain-multiplied. Pre-gain placement is fine; either order is mathematically defensible.

#### ✅ ~~2. High-pass filter at ~80 Hz~~

*Implemented.* `src/korder/audio/dsp.py:HighPassFilter` (Butterworth biquad, Direct-Form-II Transposed, state carried across chunks via `_z1`/`_z2`). Constructed per `MicRecorder` in `capture.py`; `reset()` called on stream-close so the next session starts with a zeroed delay line.

- **Where**: `src/korder/audio/capture.py:_callback`, after DC removal. Add a small DSP module `src/korder/audio/dsp.py` that holds the filter state across chunks (a stateless filter applied per chunk introduces edge-of-block clicks).
- **What**: 2nd-order Butterworth high-pass at 80 Hz, implemented as a single biquad. Coefficients are constant for a fixed cutoff and sample rate, so compute them once at recorder init. Use `scipy.signal.lfilter` with persistent `zi` state, or write the biquad by hand in numpy (~10 lines) to avoid the scipy import on the hot path.
  - Coefficients (Butterworth 80 Hz @ 16 kHz):
    - `b ≈ [0.97803, -1.95606, 0.97803]`
    - `a ≈ [1.0, -1.95558, 0.95654]`
- **Why**: Speech for transcription contains essentially nothing useful below ~80 Hz (the lowest pitch of a deep male voice is ~85 Hz fundamental, and it leaks plenty of energy into 100–300 Hz that the filter preserves). What *is* there in that band: 50/60 Hz mains hum from a desk lamp / charger, HVAC rumble, sub-bass speaker bleed during a duck-misfire, mic-stand thumps, footsteps. Whisper’s mel bins 0–2 see this as low-frequency energy and it shifts the encoder’s frame-level statistics. Standard ASR-pipeline practice.
- **Cost**: A biquad is two multiply-accumulates per sample → ~32 k MACs/sec at 16 kHz. Negligible (~30 µs per 10 ms chunk in pure numpy, sub-µs in scipy).
- **Trade-off**: The exact cutoff matters a little — 80 Hz is a defensible default; 60 Hz is more conservative for users with very low-pitched voices. Don’t go above ~100 Hz without measuring.

#### ✅ ~~3. Silence-padding trim before submission~~

*Implemented.* `vad.py:find_leading_silence` + `trim_silence(guard_ms=150)`; called from `main_window.py:_submit_transcribe` for commits and tail-flush only. Partials intentionally skip trimming — the comment in `_submit_transcribe` documents that the locked-prefix tracker requires the partial's audio origin to stay stable across re-runs.

- **Where**: `src/korder/ui/main_window.py:_submit_transcribe` (or as a helper called just before it). Already have `SpeechDetector.find_trailing_silence`; add a symmetric `find_leading_silence` and trim both ends, leaving a small (~150 ms) guard band on each side so Whisper sees transients with context.
- **What**: For commit segments and tail-flush, run webrtcvad over the buffer, find the first/last 30 ms speech frame, slice. For partials, **don’t trim** — the rolling partial flow already starts at `_committed_samples` and trimming it would invalidate the locked-prefix tracker in `_flush_pending_partial`.
- **Why**: whisper.cpp on CPU/Vulkan is famously prone to producing canned phrases (“Thanks for watching!”, “.”, “you”) when fed long pauses. Trimming silence pre/post is the cheapest and most effective way to suppress those. Bonus: shorter clips → faster transcription → better partial-vs-final UX.
- **Cost**: webrtcvad is already on the hot path; a second backwards-scan is O(n_frames). Single-digit milliseconds for a 10-second buffer.
- **Trade-off**: Trimming too aggressively can cut soft consonant onsets (`ś`, `s`, `f`) — keep the 150 ms guard. Don’t apply to live partials (would break the prefix-locking UX).

#### ✅ ~~4. Replace the static gain with a one-shot peak guard~~

*Implemented.* `dsp.py:PeakLimiter` with slow-following peak EMA (`decay=0.999` per chunk) so a single loud chunk doesn't pump the next ~200 ms. Wired into `MicRecorder.__init__` as `self._limiter`, applied in `_callback` after the HPF.

- **Where**: `src/korder/audio/capture.py:_callback`. Replace the unconditional `chunk *= self.gain` with a peak-aware version that scales down only when needed.
- **What**: Compute `peak = np.max(np.abs(chunk))`. If `peak * self.gain > 0.95`, scale by `0.95 / peak` (clip-avoiding); otherwise scale by `self.gain`. Keep a slow-following maximum across chunks to avoid pumping (`peak_ema = max(peak, 0.999 * peak_ema)`).
- **Why**: The current `gain=0.7` is a defensive constant — it’s set low so loud users don’t clip, which means quiet users feed Whisper an under-driven signal where the noise floor is closer to speech RMS. A peak-aware scale gives quiet users their full dynamic range without ever clipping loud users. Whisper’s encoder normalizes mel-bin energies so absolute level matters less than SNR, but the int16 → float32 conversion path inside whisper.cpp (and the mel-bin floor of -log) does still benefit from a well-driven signal.
- **Cost**: One `np.abs().max()` per chunk + a couple of comparisons. ~10 µs.
- **Trade-off**: The slow-following peak prevents per-chunk pumping but does mean the first ~200 ms after a sudden volume jump may be momentarily over-attenuated. Acceptable for ASR.

#### ✅ ~~5. Inject a domain `initial_prompt` automatically~~

*Implemented.* `WhisperEngine.transcribe(audio, initial_prompt=...)` accepts a per-call override (None falls back to the static config prompt; the comment notes that whisper.cpp leaks the last-passed prompt across calls so we always pass explicitly). `MainWindow._recent_transcripts: deque(maxlen=5)` holds the rolling context, populated from committed transcripts only and cleared on session end at lines 467 and 608.

- **Where**: `src/korder/transcribe/whisper_engine.py:transcribe` — accept a per-call prompt override; populate it from a small recent-utterances ring buffer maintained in `MainWindow`.
- **What**: Keep the last N (3–5) committed transcripts in a deque on `MainWindow`, join them into a single string, pass as `initial_prompt`. Optionally seed the deque on startup with a small static glossary tuned to the user’s vocabulary (`"Korder, Spotify, ydotool, PipeWire, Plasma, Vulkan, Wayland, Polski, English"`) so the first utterance benefits too. **Reset the deque on session end** — same lifetime as the LLM op-parser history (see `_stop_recording` → `clear_op_parser_history`).
- **Why**: Whisper conditions its decoder on the prompt tokens. For Polish technical vocabulary mixed with English brand names (Korder’s sweet spot), this measurably reduces the rate of mis-transliteration (e.g. `Spotify` → `spot ifaj`). Almost free at runtime — the prompt tokens are prepended to the first chunk only.
- **Cost**: Whisper has a ~224-token prompt budget; the truncation handling is in whisper.cpp already. Roughly 2-3% additional decoder time per segment for a meaningful prompt — negligible.
- **Trade-off**: A bad prompt can hurt as much as a good one helps. Keep the deque short (3–5 entries) and only populate from *committed* (not partial) transcripts so you don’t feed Whisper its own first-pass mistakes. Don’t share prompts across languages (the prompt tokens get language-tagged per the language hint).

### Moderate complexity / risk

#### ✅ ~~6. Adaptive RMS normalization (light AGC) before submission, not on the live stream~~

*Implemented.* `dsp.py:normalize_rms(target=0.05, ceiling=0.95)`; only scales up (never down), with a peak-cap that drops the boost if the lift would exceed 0.95 absolute. Called from `_submit_transcribe` immediately after `trim_silence`, before resample/handoff to Whisper. Not applied on the live `_callback` stream.

- **Where**: A new helper in `src/korder/audio/dsp.py`, called from `_submit_transcribe` immediately before handing the buffer to the worker. **Do not** apply on the live `_callback` stream — that interferes with peak-guard #4 and with webrtcvad’s threshold expectations.
- **What**: Compute `rms = sqrt(mean(audio**2))`. If `rms > 0` and `rms < 0.05`, scale by `0.05 / rms`, clipping the result at 0.95. If `rms > 0.2`, leave alone (it’s already loud enough). Only normalize *quiet* utterances upward; never normalize loud ones downward (Whisper handles loud fine; quiet is where it hallucinates).
- **Why**: Whisper’s encoder is moderately level-invariant but the mel-bin log-floor and the `_has_speech_energy` RMS gate (`whisper_engine.py:66`) both fail-quiet on under-driven utterances. A bounded one-shot scale-up before transcribe gives Whisper a more typical operating point.
- **Cost**: One pass over the buffer for RMS + one in-place multiply. Linear in segment length, ~1 ms for a 5-second clip.
- **Trade-off**: Aggressive AGC can amplify background noise when the speaker is far from the mic. The 0.05 floor is conservative; tune empirically. **Risk: this might *hurt* in cases where the user’s mic is genuinely close-talked but they’re whispering — both real speech and the silence floor get amplified, which can push silence-floor noise into Whisper’s “probably speech” region. Helps far more often than it hurts on a desk-mic setup.**

#### ✅ ~~7. Sample-rate verification + explicit resampling on mismatch~~

*Implemented.* `capture.py:_open_stream_with_retry` logs the negotiated rate and warns when PipeWire silently resamples (`PortAudio negotiated %.0f Hz (requested %d Hz)`). `dsp.py:resample_to` wraps `scipy.signal.resample_poly`, used at `main_window.py:_submit_transcribe` only when `recorder.sample_rate != 16000`. scipy is best-effort imported (no hard dep on the dictation path); falls back to passing the audio through to whisper.cpp's internal resampler when scipy is absent.

- **Where**: `src/korder/audio/capture.py:_open_stream_with_retry` — after the stream opens, log the actual sample rate PortAudio negotiated. Add a check: if a downstream consumer needs 16 kHz exactly (Whisper does, webrtcvad does), and PortAudio gave us something else, do high-quality resampling once at submission time using `scipy.signal.resample_poly` (polyphase, much better than naive `resample`).
- **Why**: PortAudio’s default is to honor the requested rate, but on PipeWire it can silently fall back to the device’s native rate (often 44.1 / 48 kHz) and rely on PipeWire’s own resampler — whose quality varies by PipeWire version. Whisper resamples internally too, but its resampler is also not guaranteed to be top-shelf. Doing one explicit, controlled `resample_poly` to 16 kHz per submission gives reproducible results.
- **Cost**: `resample_poly` for a 5-second clip from 48 → 16 kHz takes ~2 ms; trivial relative to Whisper inference.
- **Trade-off**: Adds a scipy dependency on the hot path. If the device truly is at 16 kHz natively (the default config and most USB headsets), this is a no-op check — guarded behind `if recorder.sample_rate != 16000`. Worth doing as a sanity check / log line even before adding the resampling fallback.

#### 8. Pre-emphasis (measure first; not a default)

- **Where**: Same place as the high-pass: `src/korder/audio/capture.py:_callback`.
- **What**: `y[n] = x[n] - 0.97 * x[n-1]`. State (one sample) carried across chunks.
- **Why**: Classical ASR frontends use pre-emphasis to flatten the natural -6 dB/octave roll-off of voiced speech, lifting fricative energy relative to vowels. It was essential for HMM-GMM and useful for early DNN-HMM systems. **For Whisper specifically the gain is unclear**: the model was trained on raw audio with whatever spectral tilt naturally exists, and applying pre-emphasis at inference time *changes the spectral tilt seen by the encoder relative to training*. This is a “measure first” item.
- **Cost**: One MAC per sample, sub-millisecond.
- **Trade-off**: **This might *hurt* on Whisper.** Without empirical A/B on Korder utterances, it’s speculative. Recommended ordering: measure recommendation #1–#5 first; only attempt pre-emphasis if there’s a residual fricative-confusion problem (`ś` vs `s`, `c` vs `cz`) after the cheaper steps.

### Speculative / measure first

#### 9. Channel-handling guard for stereo devices

- **Where**: `src/korder/audio/capture.py:_callback`. Currently does `chunk = indata[:, 0].copy()` — drops everything but channel 0.
- **What**: If a stereo device is selected (`indata.shape[1] > 1`), prefer downmixing rather than dropping: `chunk = indata.mean(axis=1)`. Even better: detect when one channel is silent and use only the live one (some webcams put the mic on R only and feed L silence).
- **Why**: Picking channel 0 from a webcam mic that actually feeds channel 1 gives Whisper silence and produces phantom transcriptions. Mean-mixing both halves the noise on a true stereo mic when the speech is coherent across channels.
- **Trade-off**: True stereo desk mics are rare for dictation use cases. The “silent channel detection” version costs an extra `np.abs().sum()` per channel per chunk. Worth it only if there's evidence in the field of stereo-device misbehavior.

#### 10. VAD-aware partial trimming inside the rolling-partial loop

- **Where**: `src/korder/ui/main_window.py:_on_partial_tick`, near line 636.
- **What**: Today, partials are submitted as `np.ascontiguousarray(new)` from `_committed_samples` to the end of the buffer (`main_window.py:668`). Wrap that with a leading-silence trim using `webrtcvad`: skip leading frames that contain no speech, so the partial Whisper actually sees starts at the first real speech frame.
- **Why**: A partial with 1 second of leading silence makes the partial transcript `". X"` or `"thanks. X"` — the locked-prefix renderer then has to fight the resulting noise. Leading-silence trim reduces hallucinated punctuation/canned-phrases on partials.
- **Trade-off**: The locked-prefix logic in `_flush_pending_partial` assumes the partial’s text origin is stable across re-runs; trimming the leading silence means the *audio* origin shifts forward as silence accumulates. If trimming is monotonic-only (only ever moves forward, never back), prefix locking still works. Needs careful implementation; risk of breaking partial UX if not done right. **Measure the false-positive rate of canned-phrase partials first; if it’s low, skip this — the current flow works.**

#### ✅ ~~11. Replace `_has_speech_energy` with a proper webrtcvad gate at the engine entry~~

*Implemented.* `whisper_engine.py:_has_speech_frames` replaces the global RMS gate with a per-frame webrtcvad pass: requires at least 10% speech-frame ratio AND ≥150 ms contiguous speech (the `min_speech_ratio` + `min_speech_ms` knobs match the doc's recommended thresholds). Uses a fresh `webrtcvad.Vad` instance so it doesn't share adaptive state with the detector in `MainWindow`.

- **Where**: `src/korder/transcribe/whisper_engine.py:66-70`.
- **What**: The current 0.005 RMS gate is global; it lets a *long* low-RMS clip through (a long silent recording averages to under 0.005 only if it’s very quiet). Replace with a *per-frame* speech ratio test using webrtcvad: gate transcription off if fewer than (say) 10% of 30 ms frames are speech.
- **Why**: Tightens the “don’t feed Whisper junk” bar. Same goal as #3 but at a different layer; complementary, not redundant.
- **Cost**: webrtcvad is fast — sub-millisecond per second of audio.
- **Trade-off**: Possibly redundant with #3 if both ship; pick one. The deeper gate (in `WhisperEngine`) is more defensive but also more invasive. The shallower gate (in `_submit_transcribe`) is what users will normally exercise.

## Out of scope

- **Spectral subtraction / classical noise suppression**: The user is explicitly on a *quiet desk mic with a 7800 XT*, per `README.md:45`. Spectral subtraction’s trade-off is well known: improves SNR in noisy environments at the cost of musical-noise artifacts that smear transients (especially fricatives). On a clean signal it can only hurt. Korder’s ducker already neutralizes the dominant noise source for this user (speaker bleed). If a noisy-environment user opens an issue, revisit; openwakeword’s Speex NS hook is a precedent for adding it as an opt-in.
- **Full deep-learning denoisers (RNNoise, DeepFilterNet)**: Same reasoning — disproportionate dependency footprint and CPU cost for a clean-mic baseline. RNNoise specifically tends to over-suppress soft consonants. Strong negative-default for the canonical Korder user.
- **Compression / dynamic range expansion (downward)**: Whisper handles dynamic range fine; aggressive compression flattens informative dynamics (sentence-final downstep, prosodic accents) that the language model implicitly conditions on.
- **Re-implementing Whisper’s mel filterbank ourselves**: Whisper does its own mel computation in C (whisper.cpp). Doing it in Python first would be wasted work and would risk numerical drift relative to what the encoder was trained on.
- **Per-utterance normalization to Whisper’s expected RMS**: Whisper’s training distribution is broad enough that targeting a specific RMS is overfitting to assumed model quirks. The gentler version of this idea (recommendation #6) caps gain at a sane peak and only nudges quiet utterances upward; that’s the version worth shipping.
- **Adaptive sample-rate selection per device**: Always run at 16 kHz; openwakeword needs it, webrtcvad needs it (or one of its few valid rates), and Whisper resamples internally anyway. Letting the device dictate the rate would just create one more thing that can quietly be wrong.
- **VAD using Silero / openwakeword’s VAD model**: webrtcvad is already on the hot path, faster than Silero, and accurate enough for segmentation. Switching VADs is a large change for marginal gain on this use case.

## Suggested implementation ordering

1. **#1 (DC removal)** + **#2 (HPF)** in a small `src/korder/audio/dsp.py` module — minimum diff, free wins.
2. **#3 (silence trim before submit)** — straightforward extension of existing webrtcvad usage.
3. **#5 (initial_prompt from recent transcripts)** — orthogonal to DSP, large wordlist-quality win for Polish/English mixing.
4. **#4 (peak-aware gain)** — small change to `_callback`, replaces existing constant.
5. **#7 (sample-rate verification log)** — start with just the log line; add the explicit resample only if mismatches actually appear in the wild.
6. **#6 (RMS lift for quiet utterances)** — gated behind a config knob until measured.
7. Defer #8–#11 until #1–#7 are measured against real Korder utterances.
