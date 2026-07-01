import { Chat } from "./components/Chat";
import { LlmLogsPage } from "./components/LlmLogsPage";

export default function App() {
  if (window.location.pathname === "/logs") return <LlmLogsPage />;
  return <Chat />;
}
