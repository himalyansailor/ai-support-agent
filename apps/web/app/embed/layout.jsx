import "../globals.css";

export const metadata = {
  title: "Chat",
  robots: { index: false, follow: false },
};

// Own root layout: the iframe page has no dashboard shell and no auth provider.
export default function EmbedLayout({ children }) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="h-full" style={{ background: "transparent" }}>
        {children}
      </body>
    </html>
  );
}
