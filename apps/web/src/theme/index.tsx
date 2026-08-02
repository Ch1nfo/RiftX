import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useLayoutEffect,
  useMemo,
  useState,
} from "react";

export type Theme = "dark" | "light";

const STORAGE_KEY = "riftx.theme";
const THEME_COLORS: Record<Theme, string> = {
  dark: "#07110f",
  light: "#f4f8f6",
};

function readStoredTheme(): Theme {
  if (typeof window === "undefined") return "dark";
  const stored = window.localStorage.getItem(STORAGE_KEY);
  return stored === "light" || stored === "dark" ? stored : "dark";
}

export function applyTheme(theme: Theme) {
  if (typeof document === "undefined") return;
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
  document
    .querySelector<HTMLMetaElement>('meta[name="theme-color"]')
    ?.setAttribute("content", THEME_COLORS[theme]);
}

export function initializeTheme(): Theme {
  const theme = readStoredTheme();
  applyTheme(theme);
  return theme;
}

type ThemeContextValue = {
  theme: Theme;
  setTheme: (theme: Theme) => void;
  toggleTheme: () => void;
};

const defaultContext: ThemeContextValue = {
  theme: "dark",
  setTheme: () => undefined,
  toggleTheme: () => undefined,
};

const ThemeContext = createContext<ThemeContextValue>(defaultContext);

export function ThemeProvider({
  children,
  initialTheme,
}: {
  children: ReactNode;
  initialTheme?: Theme;
}) {
  const [theme, setThemeState] = useState<Theme>(initialTheme ?? readStoredTheme);

  const setTheme = useCallback((nextTheme: Theme) => {
    applyTheme(nextTheme);
    window.localStorage.setItem(STORAGE_KEY, nextTheme);
    setThemeState(nextTheme);
  }, []);

  const toggleTheme = useCallback(() => {
    setTheme(theme === "dark" ? "light" : "dark");
  }, [setTheme, theme]);

  useLayoutEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const value = useMemo(
    () => ({ theme, setTheme, toggleTheme }),
    [setTheme, theme, toggleTheme],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  return useContext(ThemeContext);
}

export const themeStorageKey = STORAGE_KEY;
