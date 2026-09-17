import { useEffect, useLayoutEffect, useRef, useState, type RefObject, type KeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import { useI18n } from '../i18n';
import type { ThreadNode } from '../types';
import './ChatTurnNavigator.css';

export const turnKey = (turn: ThreadNode, index: number) => `${turn.node_id}:${turn.question_message_id ?? index}`;

interface Props {
  turns: ThreadNode[];
  scrollRef: RefObject<HTMLDivElement>;
  scopeKey: string;
  onJump: () => void;
}

/** A local reading index. Jumping never switches the active conversation node. */
export function ChatTurnNavigator({ turns, scrollRef, scopeKey, onJump }: Props) {
  const { t } = useI18n();
  const [active, setActive] = useState(0);
  const [focused, setFocused] = useState<number | null>(null);
  const [preview, setPreview] = useState<{ index: number; left: number; top: number } | null>(null);
  const trackRef = useRef<HTMLDivElement>(null);
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  const keys = turns.map(turnKey).join(',');

  useEffect(() => { setPreview(null); setFocused(null); }, [scopeKey, keys]);

  useLayoutEffect(() => {
    const scroller = scrollRef.current;
    if (!scroller || turns.length < 2) return;
    let frame = 0;
    function update() {
      frame = 0;
      if (!scroller) return;
      const sections = Array.from(scroller.querySelectorAll<HTMLElement>('[data-turn-key]'));
      if (!sections.length) return;
      const readingLine = scroller.getBoundingClientRect().top + Math.min(120, scroller.clientHeight * .2);
      let index = 0;
      for (let i = 0; i < sections.length; i++) {
        if (sections[i].getBoundingClientRect().top <= readingLine) index = i;
        else break;
      }
      if (scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 4) index = sections.length - 1;
      setActive(index);
    }
    function schedule() { if (!frame) frame = requestAnimationFrame(update); }
    schedule();
    scroller.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', schedule);
    const observer = new ResizeObserver(schedule);
    observer.observe(scroller);
    if (scroller.firstElementChild) observer.observe(scroller.firstElementChild);
    return () => {
      cancelAnimationFrame(frame);
      scroller.removeEventListener('scroll', schedule);
      window.removeEventListener('resize', schedule);
      observer.disconnect();
    };
  }, [scopeKey, keys, scrollRef]);

  function reveal(index: number) {
    const track = trackRef.current;
    const button = buttons.current[index];
    if (!track || !button) return;
    if (button.offsetTop < track.scrollTop) track.scrollTop = button.offsetTop;
    else if (button.offsetTop + button.offsetHeight > track.scrollTop + track.clientHeight)
      track.scrollTop = button.offsetTop + button.offsetHeight - track.clientHeight;
  }
  useEffect(() => { reveal(active); }, [active]);

  function showPreview(index: number, button: HTMLButtonElement) {
    const rect = button.getBoundingClientRect();
    const width = Math.min(310, window.innerWidth - 32);
    setPreview({ index, left: Math.max(8, Math.min(rect.right + 10, window.innerWidth - width - 8)), top: Math.max(8, Math.min(rect.top - 28, window.innerHeight - 210)) });
  }

  function jump(index: number) {
    const scroller = scrollRef.current;
    if (!scroller) return;
    const key = turnKey(turns[index], index);
    const target = Array.from(scroller.querySelectorAll<HTMLElement>('[data-turn-key]')).find(element => element.dataset.turnKey === key);
    if (!target) return;
    onJump();
    setPreview(null);
    const top = scroller.scrollTop + target.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 20;
    scroller.scrollTo({ top: Math.max(0, top), behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
    setActive(index);
    target.classList.remove('chat-turn-located');
    // Restart the short highlight when the same question is selected again.
    void target.offsetWidth;
    target.classList.add('chat-turn-located');
  }

  function keysFor(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    if (event.key === 'Escape') { setPreview(null); return; }
    let next = index;
    if (event.key === 'ArrowUp') next = Math.max(0, index - 1);
    else if (event.key === 'ArrowDown') next = Math.min(turns.length - 1, index + 1);
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = turns.length - 1;
    else return;
    event.preventDefault();
    reveal(next);
    buttons.current[next]?.focus({ preventScroll: true });
  }

  if (turns.length < 2) return null;
  const shown = preview ? turns[preview.index] : undefined;
  const excerpt = shown?.answer?.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1').replace(/[#*_`>~]/g, '').trim().slice(0, 400);
  return <nav className="chat-turn-nav" aria-label={t('问题导航', 'Question navigation')} onMouseLeave={() => setPreview(null)}>
    <div className="chat-turn-nav-track" ref={trackRef} onScroll={() => {
      const index = buttons.current.findIndex(button => button === document.activeElement);
      if (preview && index >= 0 && buttons.current[index]) showPreview(index, buttons.current[index]!);
      else setPreview(null);
    }}>
      {turns.map((turn, index) => <button key={turnKey(turn, index)} ref={element => { buttons.current[index] = element; }} type="button"
        className={`chat-turn-tick${index === active ? ' is-current' : ''}${index % 5 === 0 ? ' is-major' : ''}`}
        aria-label={t('跳到第 {number} 个问题：{question}', 'Jump to question {number}: {question}', { number: index + 1, question: turn.question || t('附件提问', 'Question with attachments') })}
        aria-current={index === active ? 'step' : undefined} tabIndex={index === (focused ?? active) ? 0 : -1}
        aria-describedby={preview?.index === index ? 'chat-turn-preview' : undefined}
        onMouseEnter={event => showPreview(index, event.currentTarget)} onFocus={event => { setFocused(index); showPreview(index, event.currentTarget); }}
        onBlur={() => { setPreview(null); setFocused(null); }} onKeyDown={event => keysFor(event, index)} onClick={() => jump(index)}><span /></button>)}
    </div>
    {preview && shown && createPortal(<div id="chat-turn-preview" className="chat-turn-preview" role="tooltip" style={{ left: preview.left, top: preview.top }}>
      <span className="chat-turn-preview-count">{t('第 {number} 个问题 · 共 {count} 个', 'Question {number} of {count}', { number: preview.index + 1, count: turns.length })}</span>
      <strong>{shown.question || t('附件提问', 'Question with attachments')}</strong>
      {excerpt && <p>{excerpt}</p>}
    </div>, document.body)}
  </nav>;
}
