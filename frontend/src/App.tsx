// 啟動流程：取得 LIFF ID → liff.init → 已有 session 就直接用；否則以 LINE ID token 登入。
// 未綁定的使用者只能看到綁定頁或說明（§4.2-5）。
import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { BrowserRouter, NavLink, Route, Routes, useLocation } from "react-router-dom";

import { api, ApiError, errorMessage, type Team, type User } from "./api";
import { Status } from "./components/Status";
import { realLiff, type LiffClient } from "./liff";
import { BindPage } from "./pages/BindPage";
import { DraftPage } from "./pages/DraftPage";
import { MyPage } from "./pages/MyPage";
import { TeamPage } from "./pages/TeamPage";

interface Session {
  user: User;
  teams: Team[];
}

interface AppContextValue {
  session: Session;
  liff: LiffClient;
}

const AppContext = createContext<AppContextValue | null>(null);

export function useApp(): AppContextValue {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error("useApp outside AppContext");
  return ctx;
}

type Boot =
  | { phase: "loading" }
  | { phase: "error"; message: string }
  | { phase: "bind"; liff: LiffClient }
  | { phase: "unbound"; liff: LiffClient }
  | { phase: "ready"; liff: LiffClient; session: Session };

export function App({ liffFactory = realLiff }: { liffFactory?: () => Promise<LiffClient> }) {
  return (
    <BrowserRouter>
      <Shell liffFactory={liffFactory} />
    </BrowserRouter>
  );
}

function Shell({ liffFactory }: { liffFactory: () => Promise<LiffClient> }) {
  const [boot, setBoot] = useState<Boot>({ phase: "loading" });
  const location = useLocation();

  const start = useCallback(async () => {
    try {
      const [liff, config] = await Promise.all([liffFactory(), api.config()]);
      if (!config.liff_id) throw new Error("LIFF_ID is not configured");
      await liff.init(config.liff_id);
      if (window.location.pathname === "/bind") {
        if (!liff.isLoggedIn()) return liff.login(window.location.href);
        return setBoot({ phase: "bind", liff });
      }
      try {
        return setBoot({ phase: "ready", liff, session: await api.me() });
      } catch (err) {
        if (!(err instanceof ApiError && err.status === 401)) throw err;
      }
      if (!liff.isLoggedIn()) return liff.login(window.location.href);
      const idToken = liff.getIDToken();
      if (!idToken) throw new Error("no LINE ID token");
      const login = await api.loginWithLine(idToken);
      if (!login.bound) return setBoot({ phase: "unbound", liff });
      setBoot({ phase: "ready", liff, session: await api.me() });
    } catch (err) {
      setBoot({ phase: "error", message: err instanceof ApiError ? errorMessage(err) : "無法啟動，請從 LINE 重新開啟。" });
    }
  }, [liffFactory]);

  useEffect(() => {
    void start();
  }, [start]);

  if (boot.phase === "loading") return <Status kind="loading" />;
  if (boot.phase === "error") return <Status kind="error" message={boot.message} />;
  if (boot.phase === "bind") return <BindPage liff={boot.liff} onBound={() => void start()} />;
  if (boot.phase === "unbound") {
    return (
      <Status
        kind="empty"
        title="尚未完成成員綁定"
        message="請使用管理員提供的邀請連結完成綁定，綁定後就能在 LINE 用一句話新增行程。"
      />
    );
  }

  return (
    <AppContext.Provider value={{ session: boot.session, liff: boot.liff }}>
      <div className="app">
        <main className="content">
          <Routes>
            <Route path="/drafts/:id" element={<DraftPage />} />
            <Route path="/team" element={<TeamPage />} />
            <Route path="*" element={<MyPage />} />
          </Routes>
        </main>
        {!location.pathname.startsWith("/drafts/") && (
          <nav className="tabbar">
            <NavLink to="/" end>我的行程</NavLink>
            <NavLink to="/team">團隊行程</NavLink>
          </nav>
        )}
      </div>
    </AppContext.Provider>
  );
}
