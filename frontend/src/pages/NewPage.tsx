// /new：圖文選單「新增行程」的入口。建立一份空白草稿後直接進入確認／編輯頁。
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api, errorMessage } from "../api";
import { Status } from "../components/Status";

export function NewPage() {
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  const started = useRef(false); // StrictMode 會執行兩次 effect；避免建立兩份草稿

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    api
      .newDraft("create")
      .then((draft) => navigate(`/drafts/${draft.id}`, { replace: true }))
      .catch((err) => setError(errorMessage(err)));
  }, [navigate]);

  if (error) return <Status kind="error" message={error} action={<Link to="/">回我的行程</Link>} />;
  return <Status kind="loading" message="建立新行程…" />;
}
