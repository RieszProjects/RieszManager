/**
 * Voice input, in order of preference:
 * - live: the microphone's audio is streamed to the server (`/ws/transcribe`) and the text appears while
 *   you speak. The recording is kept as well: if the stream fails at any point, it is uploaded instead.
 * - server: record, then upload for transcription (with the project names as keywords).
 * - browser: the browser's own Web Speech API, when the server has no transcription configured.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import { useApp } from "@/store/app";
import { toast } from "@/store/ui";

type Mode = "live" | "server" | "browser" | "none";

interface SpeechRecognitionLike {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onresult: ((ev: { resultIndex: number; results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }> }) => void) | null;
  onend: (() => void) | null;
  onerror: ((ev: { error: string }) => void) | null;
  start: () => void;
  stop: () => void;
}

function speechCtor(): (new () => SpeechRecognitionLike) | null {
  const w = window as unknown as { SpeechRecognition?: new () => SpeechRecognitionLike; webkitSpeechRecognition?: new () => SpeechRecognitionLike };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

function audioContextCtor(): typeof AudioContext | null {
  const w = window as unknown as { AudioContext?: typeof AudioContext; webkitAudioContext?: typeof AudioContext };
  return w.AudioContext ?? w.webkitAudioContext ?? null;
}

/** Names the transcription should recognise: the workspace's projects. */
const keywords = () => useApp.getState().projects.slice(0, 60).map((p) => p.name);

/** How long to wait for the final text after stopping before uploading the recording instead. */
const LIVE_FINISH_MS = 25_000;
/** Audio chunks (about 100 ms each) kept while the connection is still opening. */
const LIVE_QUEUE = 200;
/** The audio pipeline normally delivers within a few ms. When the first chunk comes later than this after the
 * microphone opened, the stream missed the first words, so the final text is taken from the whole recording. */
const LIVE_LATE_MS = 400;

interface LiveSession {
  ws: WebSocket | null;
  ctx: AudioContext | null;
  /** streaming: text is arriving; done: the final text was delivered; failed: upload the recording instead. */
  state: "streaming" | "done" | "failed";
  /** The latest text from the stream, kept if the upload fails too. */
  text: string;
  /** The stream started late and misses the beginning: show its text, but transcribe the recording at the end. */
  late: boolean;
  /** The complete recording, once the recorder has stopped. */
  blob: Blob | null;
  stopped: boolean;
  timer: number | null;
}

/**
 * `onText(text, interim)`: interim text replaces the previous interim text of the same recording; the final
 * text (possibly empty) replaces it for good. `cancel()` ends a recording and drops whatever it would still deliver.
 */
export function useVoiceInput(serverAvailable: boolean, liveAvailable: boolean, onText: (text: string, interim: boolean) => void) {
  const canRecord = serverAvailable && typeof MediaRecorder !== "undefined";
  const canStream = canRecord && liveAvailable && typeof AudioWorkletNode !== "undefined" && audioContextCtor() !== null;
  const mode: Mode = canStream ? "live" : canRecord ? "server" : speechCtor() ? "browser" : "none";
  const [recording, setRecording] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const recorder = useRef<MediaRecorder | null>(null);
  const recognition = useRef<SpeechRecognitionLike | null>(null);
  const live = useRef<LiveSession | null>(null);
  const timer = useRef<number | null>(null);
  /** Bumped by cancel(): results of recordings started before it are dropped. */
  const generation = useRef(0);
  const onTextRef = useRef(onText);
  onTextRef.current = onText;

  const stopTimer = () => {
    if (timer.current) window.clearInterval(timer.current);
    timer.current = null;
  };
  const startTimer = () => {
    setRecording(true);
    setElapsed(0);
    const started = Date.now();
    timer.current = window.setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 250);
  };

  const closeLive = (s: LiveSession) => {
    if (s.timer) window.clearTimeout(s.timer);
    s.timer = null;
    void s.ctx?.close().catch(() => undefined);
    s.ctx = null;
    if (s.ws && s.ws.readyState <= WebSocket.OPEN) s.ws.close();
  };

  useEffect(() => () => {
    stopTimer();
    recorder.current?.stream.getTracks().forEach((t) => t.stop());
    recognition.current?.stop();
    if (live.current) closeLive(live.current);
  }, []);

  /** Upload a finished recording; `fallbackText` (from a stream that failed) is kept if that fails too. */
  const upload = async (blob: Blob, fallbackText = "") => {
    const mine = generation.current;
    const deliver = (text: string) => {
      if (generation.current === mine) onTextRef.current(text, false);
    };
    if (blob.size < 2000) {
      deliver(fallbackText);
      return;
    }
    setTranscribing(true);
    const form = new FormData();
    form.append("audio", blob, blob.type.includes("mp4") ? "speech.mp4" : "speech.webm");
    for (const name of keywords()) form.append("keywords", name);
    try {
      const res = await api<{ text: string }>("transcribe", { body: form });
      deliver(res.text || fallbackText);
    } catch (err) {
      deliver(fallbackText);
      if (generation.current === mine) toast((err as Error).message, "error");
    } finally {
      if (generation.current === mine) setTranscribing(false);
    }
  };

  /** Start recording `stream`; `onDone` gets the whole recording when it stops. */
  const record = (stream: MediaStream, onDone: (blob: Blob) => void) => {
    const mime = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm"].find((m) => MediaRecorder.isTypeSupported(m)) ?? "";
    const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    const chunks: Blob[] = [];
    rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    rec.onstop = () => {
      stream.getTracks().forEach((t) => t.stop());
      onDone(new Blob(chunks, { type: rec.mimeType || "audio/webm" }));
    };
    rec.start(250);
    recorder.current = rec;
  };

  const microphone = async (): Promise<MediaStream | null> => {
    try {
      return await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      toast("无法使用麦克风", "error");
      return null;
    }
  };

  const startServer = async () => {
    const stream = await microphone();
    if (!stream) return;
    const mine = generation.current;
    record(stream, (blob) => {
      if (generation.current === mine) void upload(blob);
    });
    startTimer();
  };

  /** The stream is over one way or the other: once the recording is complete too, finish. */
  const settleLive = (s: LiveSession) => {
    if (live.current !== s || !s.stopped || s.state === "streaming" || (s.state === "failed" && !s.blob)) return;
    live.current = null;
    closeLive(s);
    if (s.state === "failed" && s.blob) void upload(s.blob, s.text);
    else setTranscribing(false);
  };

  const startLive = async () => {
    const Ctx = audioContextCtor();
    if (!Ctx) return startServer();
    // Created before the first await, while the tap still counts as a user gesture (Safari).
    let ctx: AudioContext;
    try {
      ctx = new Ctx({ sampleRate: 24000 });
    } catch {
      ctx = new Ctx();
    }
    // Loaded while the microphone is being opened, so that capturing starts with the first word.
    const worklet = ctx.resume().then(() => ctx.audioWorklet.addModule("/pcm-worklet.js"));
    worklet.catch(() => undefined);
    const stream = await microphone();
    if (!stream) {
      void ctx.close();
      return;
    }
    const opened = performance.now();
    const s: LiveSession = { ws: null, ctx, state: "streaming", text: "", late: false, blob: null, stopped: false, timer: null };
    live.current = s;
    record(stream, (blob) => {
      s.blob = blob;
      settleLive(s);
    });
    startTimer();
    const fail = () => {
      if (s.state !== "streaming") return;
      s.state = "failed";
      settleLive(s);
    };
    try {
      await worklet;
      if (live.current !== s || s.stopped) return fail();
      const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(`${proto}//${window.location.host}/ws/transcribe`);
      ws.binaryType = "arraybuffer";
      s.ws = ws;
      const queue: ArrayBuffer[] = [];
      const node = new AudioWorkletNode(ctx, "pcm-capture");
      let first = true;
      node.port.onmessage = (e: MessageEvent<ArrayBuffer>) => {
        if (first) {
          first = false;
          s.late = performance.now() - opened - 100 > LIVE_LATE_MS;   // a chunk holds 100 ms
        }
        if (s.stopped || s.state !== "streaming") return;
        if (ws.readyState === WebSocket.OPEN) ws.send(e.data);
        else if (queue.length < LIVE_QUEUE) queue.push(e.data);
      };
      // Through a silent gain to the output, so that the graph is pulled and the worklet runs.
      const mute = ctx.createGain();
      mute.gain.value = 0;
      ctx.createMediaStreamSource(stream).connect(node).connect(mute).connect(ctx.destination);
      ws.onopen = () => {
        ws.send(JSON.stringify({ type: "start", keywords: keywords() }));
        for (const chunk of queue.splice(0)) ws.send(chunk);
        if (s.stopped) ws.send(JSON.stringify({ type: "stop" }));
      };
      ws.onmessage = (e) => {
        if (typeof e.data !== "string" || s.state !== "streaming") return;
        let msg: { type?: string; text?: string };
        try {
          msg = JSON.parse(e.data);
        } catch {
          return;
        }
        if (msg.type === "text") {
          s.text = msg.text ?? "";
          onTextRef.current(s.text, true);
        } else if (msg.type === "done") {
          const text = msg.text ?? s.text;
          if (s.late) {
            s.text = text;
            fail();
            return;
          }
          s.state = "done";
          onTextRef.current(text, false);
          settleLive(s);
        } else if (msg.type === "error") {
          fail();
        }
      };
      ws.onerror = fail;
      ws.onclose = fail;
    } catch {
      fail();
    }
  };

  const stopLive = (s: LiveSession) => {
    s.stopped = true;
    setTranscribing(true);
    void s.ctx?.suspend().catch(() => undefined);
    if (s.state === "streaming" && s.late) s.state = "failed";   // the recording has the whole of it
    if (s.state === "streaming") {
      if (s.ws?.readyState === WebSocket.OPEN) s.ws.send(JSON.stringify({ type: "stop" }));
      s.timer = window.setTimeout(() => {
        if (s.state !== "streaming") return;
        s.state = "failed";
        settleLive(s);
      }, LIVE_FINISH_MS);
    }
    settleLive(s);
  };

  const startBrowser = () => {
    const Ctor = speechCtor();
    if (!Ctor) return;
    const r = new Ctor();
    const mine = generation.current;
    r.lang = navigator.language?.startsWith("zh") ? "zh-CN" : navigator.language || "zh-CN";
    r.continuous = true;
    r.interimResults = true;
    r.onresult = (ev) => {
      if (generation.current !== mine) return;
      let finalText = "";
      let interim = "";
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const res = ev.results[i];
        if (res.isFinal) finalText += res[0].transcript;
        else interim += res[0].transcript;
      }
      if (finalText) onTextRef.current(finalText, false);
      else if (interim) onTextRef.current(interim, true);
    };
    r.onerror = (ev) => {
      if (ev.error !== "aborted" && ev.error !== "no-speech") toast(`语音识别出错：${ev.error}`, "error");
    };
    r.onend = () => {
      setRecording(false);
      stopTimer();
    };
    recognition.current = r;
    r.start();
    startTimer();
  };

  const toggle = useCallback(async () => {
    if (recording) {
      stopTimer();
      setRecording(false);
      if (live.current) stopLive(live.current);
      recorder.current?.stop();
      recorder.current = null;
      recognition.current?.stop();
      recognition.current = null;
      return;
    }
    if (mode === "live") await startLive();
    else if (mode === "server") await startServer();
    else if (mode === "browser") startBrowser();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recording, mode]);

  const cancel = useCallback(() => {
    generation.current++;
    stopTimer();
    setRecording(false);
    setTranscribing(false);
    const s = live.current;
    if (s) {
      live.current = null;
      s.state = "done";
      closeLive(s);
    }
    recorder.current?.stop();   // releases the microphone; its recording is dropped
    recorder.current = null;
    recognition.current?.stop();
    recognition.current = null;
  }, []);

  return { available: mode !== "none", recording, transcribing, elapsed, toggle, cancel };
}
