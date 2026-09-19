import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { useI18n } from "../i18n";
import { copyText } from "../lib/clipboard";
import { qrMatrix } from "../lib/qrCode";
import type { PhoneAccess } from "../types";
import "./PhoneAccessPanel.css";

function QrCode({ value, label }: { value: string; label: string }) {
  const matrix = useMemo(() => { try { return qrMatrix(value); } catch { return null; } }, [value]);
  if (!matrix) return null;
  // Dark on light with a four-module quiet zone, whatever the theme: some scanners reject inverted codes.
  const size = matrix.length + 8;
  const path = matrix.flatMap((row, y) => row.map((dark, x) => (dark ? `M${x + 4} ${y + 4}h1v1h-1z` : ""))).join("");
  return <svg className="phone-qr" viewBox={`0 0 ${size} ${size}`} role="img" aria-label={label} shapeRendering="crispEdges"><rect width={size} height={size} fill="#fff" /><path d={path} fill="#000" /></svg>;
}

function CopyLine({ text, label }: { text: string; label: string }) {
  const { t } = useI18n();
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 2000);
    return () => window.clearTimeout(timer);
  }, [copied]);
  return <div className="phone-copy-line"><code>{text}</code><button className="btn" aria-label={label} onClick={() => void copyText(text).then(() => setCopied(true), () => {})}>{copied ? t("已复制", "Copied") : t("复制", "Copy")}</button></div>;
}

/** Open this workspace on a phone through the Mac's private Tailscale address. */
export function PhoneAccessPanel() {
  const { t } = useI18n();
  const [access, setAccess] = useState<PhoneAccess | null>(null);
  const [failed, setFailed] = useState(false);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setAccess(null);
    setFailed(false);
    api.phoneAccess(controller.signal).then(setAccess, () => { if (!controller.signal.aborted) setFailed(true); });
    return () => controller.abort();
  }, [revision]);

  const recheck = <button className="btn" onClick={() => setRevision(value => value + 1)} disabled={!access && !failed}>{t("重新检查", "Check again")}</button>;
  const privacy = t("只有登录同一个 Tailscale 账号的设备能打开；这台电脑需要保持开机、不睡眠。", "Only devices signed in to the same Tailscale account can open it. Keep this computer on and awake.");

  return <div className="phone-access" aria-live="polite">
    <div className="sub">{t("手机和电脑共用这里的全部记录、模型和 MCP 设置。", "Your phone shares every record, model and MCP setting with this computer.")}</div>
    {failed && <p>{t("无法检查手机访问状态。", "Could not check phone access.")}</p>}
    {!access && !failed && <p className="phone-muted">{t("正在检查 Tailscale…", "Checking Tailscale…")}</p>}
    {access?.state === "not_installed" && <ol>
      <li>{t("在这台电脑和手机上安装 Tailscale，并登录同一个账号：", "Install Tailscale on this computer and your phone, and sign in to the same account:")} <a href="https://tailscale.com/download" target="_blank" rel="noreferrer">tailscale.com/download</a></li>
      <li>{t("装好后回到这里，点「重新检查」。", "Then come back and choose Check again.")}</li>
    </ol>}
    {access?.state === "offline" && <p>{t("Tailscale 没有运行或还没登录。打开 Tailscale 并登录后，点「重新检查」。", "Tailscale is not running or not signed in. Open it, sign in, then choose Check again.")}</p>}
    {access?.state === "not_served" && access.command && <>
      <p>{t("在这台电脑的终端运行一次（配置会保留）：", "Run this once in a terminal on this computer (the setting persists):")}</p>
      <CopyLine text={access.command} label={t("复制命令", "Copy command")} />
      <p className="phone-muted">{access.host
        ? t("它只在你的 Tailscale 网络内以 https://{host} 提供学习树。完成后点「重新检查」。", "It serves LearningTree at https://{host}, inside your Tailscale network only. Then choose Check again.", { host: access.host })
        : t("完成后点「重新检查」。", "Then choose Check again.")}</p>
    </>}
    {access?.state === "ready" && access.url && <div className="phone-ready">
      <QrCode value={access.url} label={t("用手机扫码打开 {url}", "Scan to open {url} on your phone", { url: access.url })} />
      <div className="phone-ready-text">
        <p>{t("用手机相机扫码打开：", "Scan with your phone’s camera to open:")}</p>
        <CopyLine text={access.url} label={t("复制链接", "Copy link")} />
        <p className="phone-muted">{privacy}</p>
      </div>
    </div>}
    {access?.state === "public" && access.command && <div className="phone-danger" role="alert">
      <p><strong>{t("这个学习空间已通过 Tailscale Funnel 公开到互联网。", "This workspace is public on the internet through Tailscale Funnel.")}</strong> {t("学习树没有登录保护：任何人都能读取你的记录、修改模型设置，甚至通过 MCP 在这台电脑上运行程序。请立即运行：", "LearningTree has no sign-in: anyone can read your records, change model settings or run programs on this computer through MCP. Run this now:")}</p>
      <CopyLine text={access.command} label={t("复制命令", "Copy command")} />
    </div>}
    <div>{recheck}</div>
  </div>;
}
