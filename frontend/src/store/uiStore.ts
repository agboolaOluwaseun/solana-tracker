import { create } from "zustand";
import type { TimeWindow, ChannelTab, CallModalData, Strategy } from "@/types";

interface UIState {
  // Global filters
  timeWindow: TimeWindow;
  channelTab: ChannelTab;
  strategy: Strategy;  // "50" = stop-loss, "100" = normal 2x

  // Modal
  callModal: CallModalData | null;

  // Search
  searchQuery: string;

  // Actions
  setTimeWindow: (w: TimeWindow) => void;
  setChannelTab: (t: ChannelTab) => void;
  setStrategy: (s: Strategy) => void;
  openCallModal: (data: CallModalData) => void;
  closeCallModal: () => void;
  setSearchQuery: (q: string) => void;
}

export const useUIStore = create<UIState>((set) => ({
  timeWindow: "all",
  channelTab: "Consistent",
  strategy: "100",
  callModal: null,
  searchQuery: "",

  setTimeWindow: (timeWindow) => set({ timeWindow }),
  setChannelTab: (channelTab) => set({ channelTab }),
  setStrategy: (strategy) => set({ strategy }),
  openCallModal: (callModal) => set({ callModal }),
  closeCallModal: () => set({ callModal: null }),
  setSearchQuery: (searchQuery) => set({ searchQuery }),
}));
