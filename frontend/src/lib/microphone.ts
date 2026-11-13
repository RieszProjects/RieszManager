/** Which microphone the voice input records from (a per-device choice).
 *
 * "auto" (the default) avoids Bluetooth headsets: opening a headset's microphone switches it to its call
 * mode, which takes a second or two (the first words are lost) and sounds like a phone call (recognition
 * suffers, and so does whatever is playing). With the device's own microphone the headset is left alone. */
import { storage } from "./storage";

/** "auto", "system" (whatever the system picks, usually the headset), or a device id. */
export type MicChoice = string;

const KEY = "voice.mic";
export const getMicChoice = (): MicChoice => storage.get<string>(KEY, "auto");
export const setMicChoice = (choice: MicChoice) => storage.set(KEY, choice);

const BLUETOOTH = /bluetooth|蓝牙|airpods|\bbuds\b|hands-?free/i;
const OWN = /speakerphone|built-?in|internal|内置|内建|iphone|ipad|macbook|microphone array|麦克风阵列/i;

export const isBluetooth = (label: string) => BLUETOOTH.test(label);

/** The microphones by name. Empty until the microphone has been allowed once: browsers hide names before that. */
export async function listMicrophones(): Promise<MediaDeviceInfo[]> {
  const devices = await navigator.mediaDevices.enumerateDevices();
  return devices.filter((d) => d.kind === "audioinput" && d.label && d.deviceId && d.deviceId !== "default" && d.deviceId !== "communications");
}

/** The device's own microphone among `devices`: one named like it, else the first that is not Bluetooth. */
function ownMicrophone(devices: MediaDeviceInfo[]): MediaDeviceInfo | undefined {
  const others = devices.filter((d) => !isBluetooth(d.label));
  return others.find((d) => OWN.test(d.label)) ?? others[0];
}

const open = (deviceId?: string) => navigator.mediaDevices.getUserMedia({ audio: deviceId ? { deviceId: { exact: deviceId } } : true });

/** Open the microphone the user chose; falls back to the system's when that one cannot be opened. */
export async function openMicrophone(): Promise<MediaStream> {
  const choice = getMicChoice();
  if (choice === "system") return open();
  if (choice !== "auto") {
    try {
      return await open(choice);
    } catch {
      return open();   // unplugged, or its id changed
    }
  }
  const devices = await listMicrophones().catch(() => []);
  if (devices.some((d) => isBluetooth(d.label))) {
    const own = ownMicrophone(devices);
    if (own) {
      try {
        return await open(own.deviceId);
      } catch {
        /* the system's, below */
      }
    }
    return open();
  }
  const stream = await open();
  if (!devices.length && isBluetooth(stream.getAudioTracks()[0]?.label ?? "")) {
    // First use: names were hidden until now, and the system picked the headset. Switch to the device's own.
    const own = ownMicrophone(await listMicrophones().catch(() => []));
    if (own) {
      try {
        const better = await open(own.deviceId);
        stream.getTracks().forEach((t) => t.stop());
        return better;
      } catch {
        /* keep the headset */
      }
    }
  }
  return stream;
}
