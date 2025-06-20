<script lang="ts">
	import { getContext } from 'svelte';
	import { tools as _tools } from '$lib/stores';
	import Modal from '../common/Modal.svelte';
	import Switch from '../common/Switch.svelte';
	import { toast } from 'svelte-sonner';
	import { tick } from 'svelte';
	import { getTools } from '$lib/apis/tools';

	interface Tool {
		id: string;
		user_id: string;
		name: string;
		meta: {
			description: string | null;
			auth_completed: boolean;
			auth_url: string;
			manifest: Record<string, unknown>;
		};
		access_control: unknown;
		updated_at: number;
		created_at: number;
		user: unknown;
		enabled?: boolean;
	}

	export let show = false;
	export let selectedToolIds: string[] = [];

	const i18n = getContext('i18n');

	$: if (show) {
		init();
	}

	let toolsMap = {};

	const init = async () => {
		await _tools.set(await getTools(localStorage.token));

		toolsMap = $_tools.reduce((a, tool, i, arr) => {
			a[tool.id] = {
				name: tool.name,
				description: tool.meta.description,
				enabled: selectedToolIds.includes(tool.id)
			};

			return a;
		}, {});
	};

	const handleConnect = (authUrl: string) => {
		if (authUrl) {
			window.open(authUrl, '_blank');
			show = false;
		}
	};


	$: toolsList = ($_tools || []).map((tool: Tool) => ({
		...tool,
		enabled: selectedToolIds.includes(tool.id)
	}));
</script>

<Modal bind:show size="md">
	
	<div>
		<div class=" flex justify-between dark:text-gray-300 px-5 pt-4 pb-0.5">
			<div class=" text-lg font-medium self-center">{$i18n.t('Tools List')}</div>
			<button
				class="self-center"
				on:click={() => {
					show = false;
				}}
			>
				<svg
					xmlns="http://www.w3.org/2000/svg"
					viewBox="0 0 20 20"
					fill="currentColor"
					class="w-5 h-5"
				>
					<path
						d="M6.28 5.22a.75.75 0 00-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 101.06 1.06L10 11.06l3.72 3.72a.75.75 0 101.06-1.06L11.06 10l3.72-3.72a.75.75 0 00-1.06-1.06L10 8.94 6.28 5.22z"
					/>
				</svg>
			</button>
		</div>
	</div>

	<div class="px-5 py-4 space-y-4 dark:text-gray-300">
		{#each toolsList as tool}
			<div class="flex items-center justify-between">
				<div class="flex items-center gap-2">
					<span class="text-sm">{tool.name}</span>
				</div>
				
					<Switch
						state={tool.enabled}
						on:change={async (e) => {
							const state = e.detail;
							
							await tick();
							if (state) {
								if(tool.meta.auth_completed){
									selectedToolIds = [...selectedToolIds, tool.id];
								} else {
									handleConnect(tool.meta.auth_url)
								}					
							} else {
								selectedToolIds = selectedToolIds.filter((id) => id !== tool.id);
							}
						}}
					/>
				
			</div>
		{/each}
	</div>
</Modal>
