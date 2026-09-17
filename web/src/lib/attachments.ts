import { DOCUMENT_ACCEPT } from "./documentUploads";

export const ATTACHMENT_ACCEPT = `image/*,${DOCUMENT_ACCEPT}`;
export const MAX_IMAGES = 4;
export const MAX_IMAGE_BYTES = 4 * 1024 * 1024;

const imageTypes: Record<string, string> = {
  png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp",
  bmp: "image/bmp", avif: "image/avif", heic: "image/heic", heif: "image/heif", tif: "image/tiff", tiff: "image/tiff", svg: "image/svg+xml",
};

function imageType(file: Pick<File, "type" | "name">) {
  if (file.type.startsWith("image/")) return file.type;
  return (!file.type || file.type === "application/octet-stream") ? imageTypes[file.name.split(".").pop()?.toLowerCase() ?? ""] : undefined;
}

export function attachmentKind(file: Pick<File, "type" | "name">): "image" | "document" {
  return imageType(file) ? "image" : "document";
}

export function readImage(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => typeof reader.result === "string" ? resolve(reader.result) : reject(new Error("Image read failed"));
    reader.onerror = reader.onabort = () => reject(new Error("Image read failed"));
    reader.readAsDataURL(file.type.startsWith("image/") ? file : file.slice(0, file.size, imageType(file)));
  });
}

/** Reserve image slots immediately and keep async reads with their originating draft. */
export class ImageAttachmentQueue {
  private entries = new Map<string, File[]>();
  private running = new Set<string>();
  private generations = new Map<string, number>();
  constructor(private options: {
    images: (key: string) => string[];
    read: (file: File) => Promise<string>;
    complete: (key: string, image: string) => void;
    failed: (key: string, file: File) => void;
    changed: () => void;
  }) {}

  count(key: string): number { return this.entries.get(key)?.length ?? 0; }
  enqueue(key: string, file: File): "empty" | "size" | "limit" | null {
    if (!file.size) return "empty";
    if (file.size > MAX_IMAGE_BYTES) return "size";
    if (this.options.images(key).length + this.count(key) >= MAX_IMAGES) return "limit";
    this.entries.set(key, [...(this.entries.get(key) ?? []), file]);
    this.options.changed();
    void this.run(key);
    return null;
  }

  cancel(key: string) {
    this.entries.delete(key);
    this.generations.set(key, (this.generations.get(key) ?? 0) + 1);
    this.options.changed();
  }

  private async run(key: string) {
    if (this.running.has(key)) return;
    this.running.add(key);
    try {
      let file: File | undefined;
      while ((file = this.entries.get(key)?.[0])) {
        const generation = this.generations.get(key) ?? 0;
        try {
          const image = await this.options.read(file);
          if (generation === (this.generations.get(key) ?? 0)) this.options.complete(key, image);
        } catch {
          if (generation === (this.generations.get(key) ?? 0)) this.options.failed(key, file);
        }
        if (generation === (this.generations.get(key) ?? 0)) this.entries.set(key, (this.entries.get(key) ?? []).slice(1));
        this.options.changed();
      }
    } finally { this.running.delete(key); }
  }
}
