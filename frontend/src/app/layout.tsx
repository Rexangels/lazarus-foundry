import type { Metadata, Viewport } from "next";
import { Chakra_Petch, Inter, JetBrains_Mono } from "next/font/google";

import "./globals.css";

const display = Chakra_Petch({
  subsets: ["latin"],
  weight: ["500", "600", "700"],
  variable: "--font-display-face",
  display: "swap",
});
const body = Inter({ subsets: ["latin"], variable: "--font-body", display: "swap" });
const mono = JetBrains_Mono({ subsets: ["latin"], variable: "--font-forensic", display: "swap" });

export const metadata: Metadata = {
  title: { default: "Lazarus Foundry", template: "%s · Lazarus Foundry" },
  description:
    "An AI software revival foundry: it reads a dead repository, reconstructs what it was meant to become, and brings it back with verified code.",
};

export const viewport: Viewport = {
  themeColor: "#07090E",
  colorScheme: "dark",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={`${display.variable} ${body.variable} ${mono.variable}`}>
      <body className="relative bg-obsidian text-ink antialiased">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded focus:bg-slate-core focus:px-4 focus:py-2 focus:text-ink"
        >
          Skip to content
        </a>
        <div aria-hidden className="lab-grid pointer-events-none fixed inset-0 -z-10" />
        <div aria-hidden className="scanlines pointer-events-none fixed inset-0 z-40 mix-blend-overlay" />
        {children}
      </body>
    </html>
  );
}
