/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ['"Golos Text"', "system-ui", "-apple-system", "sans-serif"],
        mono: ['"JetBrains Mono"', "ui-monospace", "monospace"]
      },
      colors: {
        canvas: "var(--bg-canvas)",
        paper: "var(--bg-paper)",
        subtle: "var(--bg-subtle)",
        rail: "var(--bg-rail)",
        ink: {
          primary: "var(--ink-primary)",
          secondary: "var(--ink-secondary)",
          muted: "var(--ink-muted)"
        },
        hairline: "var(--line-hairline)",
        signal: {
          DEFAULT: "var(--signal)",
          hover: "var(--signal-hover)",
          tint: "var(--signal-tint)"
        },
        verified: "var(--state-verified)",
        warn: "var(--state-warn)",
        danger: "var(--state-danger)"
      }
    }
  },
  plugins: [require("@tailwindcss/forms")]
};
