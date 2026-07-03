import AdminNav from "@/components/AdminNav";

export default function AdminLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="min-h-screen bg-ink">
      <AdminNav />
      <main className="mx-auto max-w-[1500px] px-4 py-5 sm:px-6">{children}</main>
    </div>
  );
}
