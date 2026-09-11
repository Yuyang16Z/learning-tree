import { useState } from "react";
import { useI18n } from "../i18n";

interface Props {
  onClose: () => void;
  onCreate: (title: string) => void;
}

export function NewTreeModal({ onClose, onCreate }: Props) {
  const { t } = useI18n();
  const [title, setTitle] = useState("");

  function submit() {
    const t = title.trim();
    if (!t) return;
    onCreate(t);
  }

  return (
    <div className="overlay" onClick={onClose}>
      <div className="modal modal-sm" role="dialog" aria-modal="true" aria-label={t("新的学习", "New topic")} onClick={(e) => e.stopPropagation()}>
        <h3>{t("今天想弄懂什么？", "What would you like to understand?")}</h3>
        <input
          autoFocus
          placeholder={t("例如：AI Agent 如何工作", "For example: How do AI agents work?")}
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
            if (e.key === "Escape") onClose();
          }}
          style={{ width: "100%" }}
        />
        <div className="row-between">
          <span />
          <div style={{ display: "flex", gap: 8 }}>
            <button className="btn" onClick={onClose}>{t("取消", "Cancel")}</button>
            <button className="btn" onClick={submit} disabled={!title.trim()}>{t("创建", "Create")}</button>
          </div>
        </div>
      </div>
    </div>
  );
}
