import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";

const IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp"];
const PDF_TYPE = "application/pdf";
const ACCEPTED = [...IMAGE_TYPES, PDF_TYPE];
const MAX_IMAGE_BYTES = 10_000_000;
const MAX_PDF_BYTES = 50_000_000;

interface Staged {
  id: string;
  file: File;
  preview: string;
  note: string;
}

interface Props {
  busy: boolean;
  onStart: (launch: () => Promise<{ job: string }>) => void;
}

export function ScreenshotDrop({ busy, onStart }: Props) {
  const [staged, setStaged] = useState<Staged[]>([]);
  const [problems, setProblems] = useState<string[]>([]);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const stagedRef = useRef<Staged[]>([]);
  stagedRef.current = staged;

  const add = useCallback((files: File[]) => {
    const rejected: string[] = [];
    const fresh: Staged[] = [];
    for (const file of files) {
      const maxBytes = file.type === PDF_TYPE ? MAX_PDF_BYTES : MAX_IMAGE_BYTES;
      if (!ACCEPTED.includes(file.type)) rejected.push(`${file.name || "file"}: solo PNG, JPEG, WebP o PDF`);
      else if (file.size > maxBytes) rejected.push(`${file.name || "file"}: oltre ${maxBytes / 1_000_000} MB`);
      else fresh.push({ id: crypto.randomUUID(), file, preview: URL.createObjectURL(file), note: "" });
    }
    setProblems(rejected);
    if (fresh.length) setStaged((current) => [...current, ...fresh]);
  }, []);

  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      const files = [...(e.clipboardData?.files ?? [])].filter((f) => ACCEPTED.includes(f.type));
      if (files.length) {
        e.preventDefault();
        add(files);
      }
    };
    window.addEventListener("paste", onPaste);
    return () => window.removeEventListener("paste", onPaste);
  }, [add]);

  useEffect(() => () => stagedRef.current.forEach((s) => URL.revokeObjectURL(s.preview)), []);

  const remove = (id: string) =>
    setStaged((current) => {
      current.filter((s) => s.id === id).forEach((s) => URL.revokeObjectURL(s.preview));
      return current.filter((s) => s.id !== id);
    });

  const ingest = () => {
    const batch = staged;
    onStart(async () => {
      for (const item of batch) await api.uploadFile(item.file, item.note);
      batch.forEach((s) => URL.revokeObjectURL(s.preview));
      setStaged([]);
      return api.ingestLinks([]);
    });
  };

  return (
    <section className="card">
      <h2>Screenshot e documenti</h2>
      <div
        className={`dropzone ${dragging ? "over" : ""}`}
        role="button"
        tabIndex={0}
        aria-label="Carica screenshot o PDF: trascina qui, clicca per scegliere un file o incolla con Ctrl+V"
        onClick={() => inputRef.current?.click()}
        onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          add([...e.dataTransfer.files]);
        }}
      >
        Trascina qui screenshot e PDF, clicca per sceglierli o incollali con Ctrl+V
        <div className="muted">
          Immagini PNG, JPEG o WebP fino a 10 MB; PDF fino a 50 MB (testo o scansioni). Le immagini e le pagine
          scansionate le legge il modello di visione (<span className="mono">[llm] vision_model</span>).
        </div>
      </div>
      <input
        ref={inputRef}
        type="file"
        accept={ACCEPTED.join(",")}
        multiple
        hidden
        onChange={(e) => {
          add([...(e.target.files ?? [])]);
          e.target.value = "";
        }}
      />
      {problems.length > 0 && <div className="banner error" role="alert">{problems.map((p) => <div key={p}>{p}</div>)}</div>}
      {staged.length > 0 && (
        <ul className="staged">
          {staged.map((s) => (
            <li key={s.id}>
              {s.file.type === PDF_TYPE ? <span className="thumb pdf">PDF</span> : <img src={s.preview} alt="" className="thumb" />}
              <input
                value={s.note}
                onChange={(e) => setStaged((cur) => cur.map((x) => (x.id === s.id ? { ...x, note: e.target.value } : x)))}
                placeholder="titolo o nota (facoltativo)"
                aria-label={`Nota per ${s.file.name || "lo screenshot"}`}
              />
              <button className="link-danger" onClick={() => remove(s.id)} aria-label={`Rimuovi ${s.file.name || "lo screenshot"}`}>Rimuovi</button>
            </li>
          ))}
        </ul>
      )}
      <div className="row">
        <button className="primary" disabled={staged.length === 0 || busy} onClick={ingest}>
          {busy ? "Ingest in corso…" : `Ingerisci ${staged.length || ""} file`.trim()}
        </button>
      </div>
    </section>
  );
}
