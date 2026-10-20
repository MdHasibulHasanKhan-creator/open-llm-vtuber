/**
 * PCM capture worklet.
 *
 * The microphone arrives at the AudioContext's own rate (44.1/48 kHz). The
 * backend's ASR wants 16 kHz mono ``pcm_s16le``, so this processor:
 *
 *   1. box-averages the incoming samples (a cheap anti aliasing low pass) and
 *      decimates them to 16 kHz,
 *   2. converts to signed 16 bit little endian,
 *   3. emits fixed 100 ms blocks so the main thread only sends ~10 WebSocket
 *      frames per second instead of one per 128-sample render quantum.
 *
 * This runs on the audio thread: no allocations in the steady state, and the
 * block buffer is transferred (not copied) to the main thread.
 */

const TARGET_RATE = 16000;
const BLOCK_SIZE = 1600; // 100 ms at 16 kHz

class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();

    /** How many input samples make up one output sample. */
    this._ratio = sampleRate / TARGET_RATE;
    /** Box filter running state. */
    this._sum = 0;
    this._count = 0;
    /** Fractional input samples consumed since the last output sample. */
    this._phase = 0;

    this._block = new Int16Array(BLOCK_SIZE);
    this._filled = 0;

    this.port.onmessage = (event) => {
      if (event.data === 'flush') this._flush();
    };

    this.port.postMessage({
      type: 'ready',
      inputSampleRate: sampleRate,
      outputSampleRate: TARGET_RATE,
      blockSize: BLOCK_SIZE,
    });
  }

  _flush() {
    if (this._filled === 0) return;
    const chunk = this._block.slice(0, this._filled);
    this._filled = 0;
    this.port.postMessage({ type: 'pcm', buffer: chunk.buffer }, [chunk.buffer]);
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;

    const block = this._block;
    for (let i = 0; i < channel.length; i++) {
      this._sum += channel[i];
      this._count += 1;
      this._phase += 1;

      if (this._phase >= this._ratio) {
        this._phase -= this._ratio;
        const value = this._count > 0 ? this._sum / this._count : 0;
        this._sum = 0;
        this._count = 0;

        const clamped = value < -1 ? -1 : value > 1 ? 1 : value;
        block[this._filled++] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;

        if (this._filled === BLOCK_SIZE) this._flush();
      }
    }

    // Keep the processor alive even when the input is disconnected.
    return true;
  }
}

registerProcessor('pcm-capture', PcmCaptureProcessor);
