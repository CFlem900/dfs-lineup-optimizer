/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        ticker: {
          green: '#00c853',
          red: '#ff1744',
          bg: '#0a0e17',
          card: '#111827',
          border: '#1f2937',
          muted: '#6b7280',
        },
        market: {
          bg: '#0a0e14',
          surface: '#1a1f2e',
          border: '#2d3748',
          green: '#00ff88',
          red: '#ff4757',
          yellow: '#ffa502',
          text: '#e4e6eb',
          dim: '#8b92a8',
        },
      },
      fontFamily: {
        mono: ['JetBrains Mono', 'Fira Code', 'monospace'],
      },
    },
  },
  plugins: [],
}
