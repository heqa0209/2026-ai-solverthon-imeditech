import * as Dialog from "@radix-ui/react-dialog";
import { Building2, Heart, ListFilter, LogOut, Menu, PanelLeftClose, PanelLeftOpen, X } from "lucide-react";
import { useState, type ReactNode } from "react";
import { NavLink } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";

const navigation = [
  { to: "/announcements", label: "전체 공고", icon: ListFilter },
  { to: "/interests", label: "관심 공고", icon: Heart },
  { to: "/company", label: "기업정보", icon: Building2 },
];

function Navigation({ onSelect, compact = false }: { onSelect?: () => void; compact?: boolean }) {
  const { user, logout } = useAuth();
  return (
    <>
      <div className="brand-row">
        <span className="brand-mark" aria-hidden="true">IMT</span>
        {!compact && <div><strong>지원사업 판정</strong><small>기업마당 공고를 근거로 확인합니다</small></div>}
      </div>
      <nav className="main-nav" aria-label="주요 메뉴">
        {navigation.map(({ to, label, icon: Icon }) => (
          <NavLink key={to} to={to} onClick={onSelect} className={({ isActive }) => isActive ? "active" : ""} title={compact ? label : undefined}>
            <Icon size={19} aria-hidden="true" /><span className={compact ? "sr-only" : ""}>{label}</span>
          </NavLink>
        ))}
      </nav>
      <div className="sidebar-account">
        {!compact && <div><span>로그인 계정</span><strong>{user?.username}</strong></div>}
        <button className="icon-button" type="button" onClick={() => void logout()} aria-label="로그아웃" title="로그아웃"><LogOut size={18} /></button>
      </div>
    </>
  );
}

export function AppShell({ title, description, children, actions }: { title: string; description?: string; children: ReactNode; actions?: ReactNode }) {
  const [collapsed, setCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  return (
    <div className={`app-layout ${collapsed ? "sidebar-collapsed" : ""}`}>
      <aside className="desktop-sidebar">
        <Navigation compact={collapsed} />
        <button className="collapse-button" type="button" onClick={() => setCollapsed((value) => !value)} aria-label={collapsed ? "사이드바 펼치기" : "사이드바 접기"}>
          {collapsed ? <PanelLeftOpen size={18} /> : <><PanelLeftClose size={18} /><span>사이드바 접기</span></>}
        </button>
      </aside>
      <div className="app-content">
        <header className="mobile-header">
          <Dialog.Root open={mobileOpen} onOpenChange={setMobileOpen}>
            <Dialog.Trigger asChild><button className="icon-button" aria-label="메뉴 열기"><Menu /></button></Dialog.Trigger>
            <Dialog.Portal>
              <Dialog.Overlay className="dialog-overlay" />
              <Dialog.Content className="mobile-sheet" aria-describedby={undefined}>
                <Dialog.Title className="sr-only">메뉴</Dialog.Title>
                <Dialog.Close className="dialog-close" aria-label="메뉴 닫기"><X /></Dialog.Close>
                <Navigation onSelect={() => setMobileOpen(false)} />
              </Dialog.Content>
            </Dialog.Portal>
          </Dialog.Root>
          <strong>지원사업 판정</strong>
        </header>
        <main className="page-wrap">
          <div className="page-heading">
            <div><h1>{title}</h1>{description && <p>{description}</p>}</div>
            {actions && <div className="page-actions">{actions}</div>}
          </div>
          {children}
        </main>
        <footer className="privacy-note">저장된 기업정보 전체와 공고 내용은 자격조건 분석을 위해 Codex로 전송될 수 있습니다.</footer>
      </div>
    </div>
  );
}
