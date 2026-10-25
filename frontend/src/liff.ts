// LIFF SDK 的最小介面；測試時注入假的實作。
export interface LiffClient {
  init(liffId: string): Promise<void>;
  isLoggedIn(): boolean;
  login(redirectUri: string): void;
  getIDToken(): string | null;
  closeWindow(): void;
  isInClient(): boolean;
}

export async function realLiff(): Promise<LiffClient> {
  const { default: liff } = await import("@line/liff");
  return {
    init: (liffId) => liff.init({ liffId }),
    isLoggedIn: () => liff.isLoggedIn(),
    login: (redirectUri) => liff.login({ redirectUri }),
    getIDToken: () => liff.getIDToken(),
    closeWindow: () => liff.closeWindow(),
    isInClient: () => liff.isInClient(),
  };
}
