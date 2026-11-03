/**
 * Voice input. Prefers server-side transcription (record → upload) when the
 * backend has it configured; otherwise falls back to the browser's Web Speech API.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import { toast } from "@/store/ui";

type Mode = "server" | "browser" | "none";

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

export function useVoiceInput(serverAvailable: boolean, onText: (text: string, interim: boolean) => void) {
  const mode: Mode = serverAvailable && typeof MediaRecorder !== "undefined" ? "server" : speechCtor() ? "browser" : "none";
  const [recording, setRecording] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const recorder = useRef<MediaRecorder | null>(null);
  const recognition = useRef<SpeechRecognitionLike | null>(null);
  const chunks = useRef<Blob[]>([]);
  const timer = useRef<number | null>(null);
  const onTextRef = useRef(onText);
  onTextRef.current = onText;

  const stopTimer = () => {
    if (timer.current) window.clearInterval(timer.current);
    timer.current = null;
  };

  useEffect(() => () => {
    stopTimer();
    recorder.current?.stream.getTracks().forEach((t) => t.stop());
    recognition.current?.stop();
  }, []);

  const startServer = async () => {
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      toast("无法使用麦克风", "error");
      return;
    }
    const mime = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm"].find((m) => MediaRecorder.isTypeSupported(m)) ?? "";
    const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    chunks.current = [];
    rec.ondataavailable = (e) => e.data.size && chunks.current.push(e.data);
    rec.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      const type = rec.mimeType || "audio/webm";
      const blob = new Blob(chunks.current, { type });
      if (blob.size < 2000) return;
      setTranscribing(true);
      const form = new FormData();
      form.append("audio", blob, type.includes("mp4") ? "speech.mp4" : "speech.webm");
      try {
        const res = await api<{ text: string }>("transcribe", { body: form });
        if (res.text) onTextRef.current(res.text, false);
      } catch (err) {
        toast((err as Error).message, "error");
      } finally {
        setTranscribing(false);
      }
    };
    rec.start(250);
    recorder.current = rec;
    setRecording(true);
    setElapsed(0);
    const started = Date.now();
    timer.current = window.setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 250);
  };

  const startBrowser = () => {
    const Ctor = speechCtor();
    if (!Ctor) return;
    const r = new Ctor();
    r.lang = navigator.language?.startsWith("zh") ? "zh-CN" : navigator.language || "zh-CN";
    r.continuous = true;
    r.interimResults = true;
    r.onresult = (ev) => {
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
    setRecording(true);
    setElapsed(0);
    const started = Date.now();
    timer.current = window.setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 250);
  };

  const toggle = useCallback(async () => {
    if (recording) {
      stopTimer();
      setRecording(false);
      recorder.current?.stop();
      recorder.current = null;
      recognition.current?.stop();
      recognition.current = null;
      return;
    }
    if (mode === "server") await startServer();
    else if (mode === "browser") startBrowser();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recording, mode]);

  return { available: mode !== "none", recording, transcribing, elapsed, toggle };
}
