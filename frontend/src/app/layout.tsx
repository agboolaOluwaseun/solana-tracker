import type { Metadata } from "next";
import "./globals.css";
import Navbar from "@/components/Navbar";
import CallModal from "@/components/CallModal";

export const metadata: Metadata = {
  title: "KOLfi - Solana KOL Tracker",
  description: "Track crypto KOL call performance on Solana",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <head>
        <link
          rel="preconnect"
          href="https://fonts.googleapis.com"
        />
        <link
          rel="preconnect"
          href="https://fonts.gstatic.com"
          crossOrigin=""
        />
        <link
          href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>
        <Navbar />
        <main className="mx-auto max-w-7xl px-6 py-8">
          {children}
        </main>
        <CallModal />
      </body>
    </html>
  );
}
