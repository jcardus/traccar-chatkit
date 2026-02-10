import Home from "./components/Home";
import { useColorScheme } from "./hooks/useColorScheme";

export default function App() {
  const { scheme } = useColorScheme();
  return <Home scheme={scheme} />;
}