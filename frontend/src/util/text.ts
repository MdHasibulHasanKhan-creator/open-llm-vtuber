/**
 * Text helpers for the chat transcript.
 *
 * The model is asked to open a sentence with an emotion tag (`[happy] ...`) and
 * may use gesture notes (`(laughs)`). Those markers are stripped from the text
 * we speak, but the backend keeps them in `display_text` so the UI can show
 * *why* the avatar pulled a face. Here we turn them into something friendly.
 */

const MARKER_RE = /\[([a-zA-Z_\u3040-\u30ff\u4e00-\u9fff][\w\u3040-\u30ff\u4e00-\u9fff-]{1,19})\]|\(([a-zA-Z ,'-]{2,32})\)|\*([a-zA-Z ,'-]{2,32})\*/g;

/** Emotion / gesture name -> emoji chip. */
const CHIPS: Record<string, string> = {
  happy: '😊',
  joy: '😊',
  excited: '🤩',
  laugh: '😄',
  laughs: '😄',
  laughing: '😄',
  giggle: '😄',
  smile: '🙂',
  smiles: '🙂',
  sad: '😢',
  sigh: '😮‍💨',
  sighs: '😮‍💨',
  cry: '😭',
  angry: '😠',
  annoyed: '😤',
  relaxed: '😌',
  calm: '😌',
  blush: '😳',
  blushes: '😳',
  shy: '😊',
  surprised: '😲',
  wow: '😲',
  confused: '😕',
  curious: '🤔',
  think: '🤔',
  thinking: '🤔',
  nod: '🙂',
  nods: '🙂',
  shake: '🙅',
  tilt: '🤨',
  wave: '👋',
  waves: '👋',
  lean: '🙃',
  neutral: '',
};

export type TextSegment =
  | { kind: 'text'; value: string }
  | { kind: 'chip'; value: string; label: string };

/**
 * Split a displayed reply into plain text and emoji chips. Unknown markers are
 * dropped (the model occasionally invents one).
 */
export function splitMarkers(text: string): TextSegment[] {
  const segments: TextSegment[] = [];
  let last = 0;
  MARKER_RE.lastIndex = 0;

  for (let match = MARKER_RE.exec(text); match !== null; match = MARKER_RE.exec(text)) {
    const before = text.slice(last, match.index);
    if (before) segments.push({ kind: 'text', value: before });

    const label = (match[1] ?? match[2] ?? match[3] ?? '').trim().toLowerCase();
    const emoji = CHIPS[label];
    if (emoji) segments.push({ kind: 'chip', value: emoji, label });

    last = match.index + match[0].length;
  }

  const tail = text.slice(last);
  if (tail) segments.push({ kind: 'text', value: tail });
  return segments;
}

/** Plain text without any markers - used for the live streaming preview. */
export function stripMarkers(text: string): string {
  return text
    .replace(MARKER_RE, ' ')
    .replace(/\s{2,}/g, ' ')
    .trim();
}

/** Trim a long transcript for a tooltip or a status line. */
export function ellipsis(text: string, max = 90): string {
  const trimmed = text.trim();
  return trimmed.length <= max ? trimmed : `${trimmed.slice(0, max - 1)}…`;
}
