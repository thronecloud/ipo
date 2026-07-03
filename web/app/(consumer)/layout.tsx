import TopBar from "@/components/TopBar";

export default function ConsumerLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="min-h-screen bg-ink">
      <TopBar />
      <main className="mx-auto max-w-[1500px] px-4 py-5 sm:px-6">{children}</main>
    </div>
  );
}
