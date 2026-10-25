// 綁定頁：先顯示邀請對應的成員姓名，本人確認「這是我」後才兌換（§4.3、P10）。
import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { api, errorMessage } from "../api";
import { Status } from "../components/Status";
import type { LiffClient } from "../liff";

type State =
  | { step: "loading" }
  | { step: "confirm"; name: string; organization: string }
  | { step: "binding"; name: string; organization: string }
  | { step: "declined" }
  | { step: "error"; message: string };

export function BindPage({ liff, onBound }: { liff: LiffClient; onBound: () => void }) {
  const [params] = useSearchParams();
  const token = params.get("invite") ?? "";
  const navigate = useNavigate();
  const [state, setState] = useState<State>({ step: "loading" });

  useEffect(() => {
    if (!token) {
      setState({ step: "error", message: "連結缺少邀請碼，請使用管理員提供的完整連結。" });
      return;
    }
    api
      .previewInvitation(token)
      .then((p) => setState({ step: "confirm", name: p.display_name, organization: p.organization }))
      .catch((err) => setState({ step: "error", message: errorMessage(err) }));
  }, [token]);

  async function bind(name: string, organization: string) {
    setState({ step: "binding", name, organization });
    try {
      const idToken = liff.getIDToken();
      if (!idToken) throw new Error("no id token");
      await api.redeemInvitation(token, idToken);
      navigate("/", { replace: true }); // 邀請碼不留在網址與瀏覽紀錄
      onBound();
    } catch (err) {
      setState({ step: "error", message: errorMessage(err) });
    }
  }

  if (state.step === "loading") return <Status kind="loading" message="確認邀請中…" />;
  if (state.step === "error") return <Status kind="error" title="無法完成綁定" message={state.message} />;
  if (state.step === "declined") {
    return (
      <Status
        kind="empty"
        title="已取消"
        message="這個邀請沒有被使用。若姓名不正確，請聯絡管理員重新發送。"
        action={liff.isInClient() && <button onClick={() => liff.closeWindow()}>關閉</button>}
      />
    );
  }
  return (
    <div className="card bind">
      <p className="muted">{state.organization}</p>
      <h1>您是「{state.name}」嗎？</h1>
      <p>確認後，這個 LINE 帳號會綁定到這位成員，之後就能在 LINE 用一句話新增、查詢行程。</p>
      <p className="muted">
        行程預設只讓同團隊的成員看到「忙碌」時段；分享對象是目前的團隊成員，包含日後加入的人。
      </p>
      <div className="actions">
        <button className="primary" disabled={state.step === "binding"} onClick={() => bind(state.name, state.organization)}>
          {state.step === "binding" ? "綁定中…" : "這是我，完成綁定"}
        </button>
        <button disabled={state.step === "binding"} onClick={() => setState({ step: "declined" })}>
          不是我
        </button>
      </div>
    </div>
  );
}
