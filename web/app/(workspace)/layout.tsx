import WorkspaceSidebar from "@/components/sidebar/WorkspaceSidebar";
import { CapabilityAccessProvider } from "@/components/access/CapabilityAccessContext";
import CapabilityGate from "@/components/access/CapabilityGate";
import { UnifiedChatProvider } from "@/context/UnifiedChatContext";

export default function WorkspaceLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <CapabilityAccessProvider>
      <UnifiedChatProvider>
        <div className="flex h-screen min-w-0 overflow-hidden">
          <div className="hidden shrink-0 sm:block">
            <WorkspaceSidebar />
          </div>
          <main className="min-w-0 flex-1 overflow-hidden bg-[var(--background)]">
            <CapabilityGate>{children}</CapabilityGate>
          </main>
        </div>
      </UnifiedChatProvider>
    </CapabilityAccessProvider>
  );
}
