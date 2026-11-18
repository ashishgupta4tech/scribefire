import type { Metadata } from "next";
import {
  Geist,
  Geist_Mono,
  Pinyon_Script,
  // Display faces (headline "lead")
  Instrument_Sans,
  Bricolage_Grotesque,
  Fraunces,
  Sora,
  // Script faces (headline "accent")
  Instrument_Serif,
  Playfair_Display,
  Caveat,
  Gloock,
} from "next/font/google";
import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });
const script = Pinyon_Script({
  variable: "--font-pinyon",
  subsets: ["latin"],
  weight: "400",
});

/* ---- Font experiment: five pairings, switchable via data-font ---- */
const instrumentSans = Instrument_Sans({
  variable: "--f-instrument-sans",
  subsets: ["latin"],
});
const bricolage = Bricolage_Grotesque({
  variable: "--f-bricolage",
  subsets: ["latin"],
});
const fraunces = Fraunces({ variable: "--f-fraunces", subsets: ["latin"] });
const sora = Sora({ variable: "--f-sora", subsets: ["latin"] });

const instrumentSerif = Instrument_Serif({
  variable: "--f-instrument-serif",
  subsets: ["latin"],
  weight: "400",
  style: ["normal", "italic"],
});
const playfair = Playfair_Display({
  variable: "--f-playfair",
  subsets: ["latin"],
  style: ["normal", "italic"],
});
const caveat = Caveat({ variable: "--f-caveat", subsets: ["latin"] });
const gloock = Gloock({ variable: "--f-gloock", subsets: ["latin"], weight: "400" });

const THEME_INIT = `(function(){try{var t=localStorage.getItem('scribefire-theme'),p=localStorage.getItem('scribefire-palette'),f=localStorage.getItem('scribefire-font');if(!t)t='dark';document.documentElement.setAttribute('data-theme',t);if(p)document.documentElement.setAttribute('data-palette',p);document.documentElement.setAttribute('data-font',f||'editorial');}catch(e){}})();`;

export const metadata: Metadata = {
  title: "Scribefire — Vikas Vivan's favorite AI tool",
  description:
    "Scribefire is a free voice-to-text app that turns everything you say into clean, formatted writing — in any app on your machine.",
  applicationName: "Scribefire",
  keywords: [
    "Scribefire",
    "voice to text",
    "speech to text",
    "dictation app",
    "push to talk",
    "Whisper",
    "offline transcription",
    "macOS",
    "Windows",
  ],
  openGraph: {
    title: "Scribefire — Vikas Vivan's favorite AI tool",
    description:
      "Scribefire is a free voice-to-text app that turns everything you say into clean, formatted writing — in any app on your machine.",
    siteName: "Scribefire",
    type: "website",
  },
  twitter: {
    card: "summary",
    title: "Scribefire — Vikas Vivan's favorite AI tool",
    description:
      "Free voice-to-text that turns everything you say into clean, formatted writing, in any app on your machine.",
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html
      lang="en"
      className={`h-full antialiased ${geistSans.variable} ${geistMono.variable} ${script.variable} ${instrumentSans.variable} ${bricolage.variable} ${fraunces.variable} ${sora.variable} ${instrumentSerif.variable} ${playfair.variable} ${caveat.variable} ${gloock.variable}`} data-theme="dark" data-palette="lime" data-font="editorial" suppressHydrationWarning>
      <head>
        {/* Applies the saved theme before first paint so there is no flash. */}
        <script
          suppressHydrationWarning
          // eslint-disable-next-line react/no-danger
          dangerouslySetInnerHTML={{ __html: THEME_INIT }}
        />
      </head>
      <body
        className="min-h-full"
      >
        {children}
      </body>
    </html>
  );
}
