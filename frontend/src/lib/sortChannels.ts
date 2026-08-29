import type { ChannelCardData, ChannelTab } from "@/types";

/**
 * Sort channels based on the active tab:
 * - "Hot": longest streak (desc)
 * - "Consistent": highest win rate (desc)
 * - "New": most recently added (desc by created_at)
 * - "All": no sorting (original order)
 */
export function sortChannels(
  channels: ChannelCardData[],
  tab: ChannelTab
): ChannelCardData[] {
  const sorted = [...channels];

  switch (tab) {
    case "Hot":
      return sorted.sort((a, b) => b.streak - a.streak);

    case "Consistent":
      return sorted.sort((a, b) => b.win_rate - a.win_rate);

    case "New":
      return sorted.sort((a, b) => {
        const dateA = a.created_at ? new Date(a.created_at).getTime() : 0;
        const dateB = b.created_at ? new Date(b.created_at).getTime() : 0;
        return dateB - dateA;
      });

    case "All":
    default:
      return sorted;
  }
}
