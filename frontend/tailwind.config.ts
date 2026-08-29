import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        bg: {
          primary: "#0a0a0a",
          card: "#161b22",
          cardHover: "#1c2128",
          border: "rgba(255,255,255,0.06)",
          borderHover: "rgba(255,255,255,0.12)",
        },
        accent: {
          gold: "#f59e0b",
          goldDim: "rgba(245,158,11,0.15)",
          teal: "#00C19F",
          tealDim: "rgba(0,193,159,0.12)",
          cyan: "#3B82F6",
          red: "#EF4444",
          redDim: "rgba(239,68,68,0.15)",
        },
        text: {
          primary: "#f0f0f0",
          secondary: "rgba(255,255,255,0.6)",
          muted: "rgba(255,255,255,0.35)",
        },
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
