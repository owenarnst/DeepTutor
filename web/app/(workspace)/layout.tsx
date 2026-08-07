import ResponsiveWorkspaceNavigation from "@/components/sidebar/ResponsiveWorkspaceNavigation";
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
        <div className="flex h-screen min-w-0 flex-col overflow-hidden sm:flex-row">
          <ResponsiveWorkspaceNavigation />
          <main className="min-h-0 min-w-0 flex-1 overflow-hidden bg-[var(--background)]">
            <CapabilityGate>{children}</CapabilityGate>
          </main>
        </div>
      </UnifiedChatProvider>
    </CapabilityAccessProvider>
  );
}
