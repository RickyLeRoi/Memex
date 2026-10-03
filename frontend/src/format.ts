export function formatDate(iso: string | null): string {
  if (!iso) return "mai";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString("it-IT", { dateStyle: "short", timeStyle: "short" });
}

export interface ParsedLink {
  url: string;
  note: string;
  valid: boolean;
  raw: string;
}

/** One link per line; anything after the URL (separated by whitespace) is the note/title. */
export function parseLinks(text: string): ParsedLink[] {
  return text
    .split(/\r?\n/)
    .map((raw) => raw.trim())
    .filter((raw) => raw && !raw.startsWith("#"))
    .map((raw) => {
      const [url, ...rest] = raw.split(/\s+/);
      return { url, note: rest.join(" "), valid: /^https?:\/\/\S+$/i.test(url), raw };
    });
}
