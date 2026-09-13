import { create } from "zustand";
import type { TimeWindow, ChannelTab, CallModalData, Strategy, Chain } from "@/types";

interface UIState {
  // Global filters
  timeWindow: TimeWindow;
  channelTab: ChannelTab;
  strategy: Strategy;  // "50" = stop-loss, "100" = normal 2x
  chain: Chain;        // global chain filter (list pages)
  deepDiveChain: Chain; // chain shown inside a channel deep-dive

  // Modal
  callModal: CallModalData | null;

  // Search
  searchQuery: string;

  // Actions
  setTimeWindow: (w: TimeWindow) => void;
  setChannelTab: (t: ChannelTab) => void;
  setStrategy: (s: Strategy) => void;
  setChain: (c: Chain) => void;
  setDeepDiveChain: (c: Chain) => void;
  openCallModal: (data: CallModalData) => void;
  closeCallModal: () => void;
  setSearchQuery: (q: string) => void;
}

export const useUIStore = create<UIState>((set) => ({
  timeWindow: "all",
  channelTab: "Consistent",
  strategy: "100",
  chain: "sol",
  deepDiveChain: "sol",
  callModal: null,
  searchQuery: "",

  setTimeWindow: (timeWindow) => set({ timeWindow }),
  setChannelTab: (channelTab) => set({ channelTab }),
  setStrategy: (strategy) => set({ strategy }),
  setChain: (chain) => set({ chain }),
  setDeepDiveChain: (deepDiveChain) => set({ deepDiveChain }),
  openCallModal: (callModal) => set({ callModal }),
  closeCallModal: () => set({ callModal: null }),
  setSearchQuery: (searchQuery) => set({ searchQuery }),
}));
