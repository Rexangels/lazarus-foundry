import { redirect } from "next/navigation";

/** The command center opens on the Autopsy. */
export default async function ProjectIndex({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = await params;
  redirect(`/foundry/${encodeURIComponent(projectId)}/autopsy`);
}
