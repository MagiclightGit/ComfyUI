"""
    This file is part of ComfyUI.
    Copyright (C) 2024 Comfy

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

from typing import Dict, List, Tuple, Optional, Callable
import torch
import copy
import inspect
import logging
import uuid
import collections
import math

import comfy.utils
import comfy.float
import comfy.model_management
import comfy.lora
import comfy.hooks
from comfy.comfy_types import UnetWrapperFunction

def string_to_seed(data):
    crc = 0xFFFFFFFF
    for byte in data:
        if isinstance(byte, str):
            byte = ord(byte)
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xEDB88320
            else:
                crc >>= 1
    return crc ^ 0xFFFFFFFF

def set_model_options_patch_replace(model_options, patch, name, block_name, number, transformer_index=None):
    to = model_options["transformer_options"].copy()

    if "patches_replace" not in to:
        to["patches_replace"] = {}
    else:
        to["patches_replace"] = to["patches_replace"].copy()

    if name not in to["patches_replace"]:
        to["patches_replace"][name] = {}
    else:
        to["patches_replace"][name] = to["patches_replace"][name].copy()

    if transformer_index is not None:
        block = (block_name, number, transformer_index)
    else:
        block = (block_name, number)
    to["patches_replace"][name][block] = patch
    model_options["transformer_options"] = to
    return model_options

def set_model_options_post_cfg_function(model_options, post_cfg_function, disable_cfg1_optimization=False):
    model_options["sampler_post_cfg_function"] = model_options.get("sampler_post_cfg_function", []) + [post_cfg_function]
    if disable_cfg1_optimization:
        model_options["disable_cfg1_optimization"] = True
    return model_options

def set_model_options_pre_cfg_function(model_options, pre_cfg_function, disable_cfg1_optimization=False):
    model_options["sampler_pre_cfg_function"] = model_options.get("sampler_pre_cfg_function", []) + [pre_cfg_function]
    if disable_cfg1_optimization:
        model_options["disable_cfg1_optimization"] = True
    return model_options

def create_hook_patches_clone(orig_hook_patches):
    new_hook_patches = {}
    for hook_ref in orig_hook_patches:
        new_hook_patches[hook_ref] = {}
        for k in orig_hook_patches[hook_ref]:
            new_hook_patches[hook_ref][k] = orig_hook_patches[hook_ref][k][:]
    return new_hook_patches

def wipe_lowvram_weight(m):
    if hasattr(m, "prev_comfy_cast_weights"):
        m.comfy_cast_weights = m.prev_comfy_cast_weights
        del m.prev_comfy_cast_weights
    m.weight_function = None
    m.bias_function = None

class LowVramPatch:
    def __init__(self, key, patches):
        self.key = key
        self.patches = patches
    def __call__(self, weight):
        return comfy.lora.calculate_weight(self.patches[self.key], weight, self.key, intermediate_dtype=weight.dtype)

class CallbacksMP:
    ON_CLONE = "on_clone"
    ON_LOAD = "on_load_after"
    ON_CLEANUP = "on_cleanup"
    ON_PRE_RUN = "on_pre_run"
    ON_PREPARE_STATE = "on_prepare_state"
    ON_APPLY_HOOKS = "on_apply_hooks"
    ON_REGISTER_ALL_HOOK_PATCHES = "on_register_all_hook_patches"

    @classmethod
    def init_callbacks(cls):
        return {
            cls.ON_CLONE: [],
            cls.ON_LOAD: [],
            cls.ON_CLEANUP: [],
            cls.ON_PRE_RUN: [],
            cls.ON_PREPARE_STATE: [],
            cls.ON_APPLY_HOOKS: [],
            cls.ON_REGISTER_ALL_HOOK_PATCHES: [],
        }

class ModelPatcher:
    def __init__(self, model, load_device, offload_device, size=0, weight_inplace_update=False):
        self.size = size
        self.model = model
        if not hasattr(self.model, 'device'):
            logging.debug("Model doesn't have a device attribute.")
            self.model.device = offload_device
        elif self.model.device is None:
            self.model.device = offload_device

        self.patches = {}
        self.backup = {}
        self.object_patches = {}
        self.object_patches_backup = {}
        self.model_options = {"transformer_options":{}}
        self.model_size()
        self.load_device = load_device
        self.offload_device = offload_device
        self.weight_inplace_update = weight_inplace_update
        self.patches_uuid = uuid.uuid4()

        self.attachments: Dict[str] = {}
        self.additional_models: list[ModelPatcher] = []
        self.callbacks: Dict[str, List[Callable]] = CallbacksMP.init_callbacks()

        self.hook_patches: Dict[comfy.hooks._HookRef] = {}
        self.hook_patches_backup: Dict[comfy.hooks._HookRef] = {}
        self.hook_backup: Dict[str, Tuple[torch.Tensor, torch.device]] = {}
        self.cached_hook_patches: Dict[comfy.hooks.HookGroup, Dict[str, torch.Tensor]] = {}
        self.current_hooks: Optional[comfy.hooks.HookGroup] = None
        self.forced_hooks: Optional[comfy.hooks.HookGroup] = None  # NOTE: only used for CLIP
        # TODO: hook_mode should be entirely removed; behavior should be determined by remaining VRAM/memory
        self.hook_mode = comfy.hooks.EnumHookMode.MaxSpeed

        if not hasattr(self.model, 'model_loaded_weight_memory'):
            self.model.model_loaded_weight_memory = 0

        if not hasattr(self.model, 'lowvram_patch_counter'):
            self.model.lowvram_patch_counter = 0

        if not hasattr(self.model, 'model_lowvram'):
            self.model.model_lowvram = False

    def model_size(self):
        if self.size > 0:
            return self.size
        self.size = comfy.model_management.module_size(self.model)
        return self.size

    def loaded_size(self):
        return self.model.model_loaded_weight_memory

    def lowvram_patch_counter(self):
        return self.model.lowvram_patch_counter

    def clone(self):
        n = ModelPatcher(self.model, self.load_device, self.offload_device, self.size, weight_inplace_update=self.weight_inplace_update)
        n.patches = {}
        for k in self.patches:
            n.patches[k] = self.patches[k][:]
        n.patches_uuid = self.patches_uuid

        n.object_patches = self.object_patches.copy()
        n.model_options = copy.deepcopy(self.model_options)
        n.backup = self.backup
        n.object_patches_backup = self.object_patches_backup

        # attachments
        n.attachments = {}
        for k in self.attachments:
            if hasattr(self.attachments[k], "on_model_patcher_clone"):
                n.attachments[k] = self.attachments[k].on_model_patcher_clone()
            else:
                n.attachments[k] = self.attachments[k]
        # additional models
        for m in self.additional_models:
            n.additional_models.append(m.clone())
        # callbacks
        for k, c in self.callbacks.items():
            n.callbacks[k] = c.copy()
        # hooks
        n.hook_patches = create_hook_patches_clone(self.hook_patches)
        n.hook_patches_backup = create_hook_patches_clone(self.hook_patches_backup)
        # TODO: do we really need to clone cached_hook_patches/current_hooks?
        for group in self.cached_hook_patches:
            n.cached_hook_patches[group] = {}
            for k in self.cached_hook_patches[group]:
                n.cached_hook_patches[group][k] = self.cached_hook_patches[group][k]
        n.hook_backup = self.hook_backup
        n.current_hooks = self.current_hooks.clone() if self.current_hooks else self.current_hooks
        n.forced_hooks = self.forced_hooks.clone() if self.forced_hooks else self.forced_hooks
        n.hook_mode = self.hook_mode

        for callback in self.callbacks[CallbacksMP.ON_CLONE]:
            callback(self, n)
        return n

    def is_clone(self, other):
        if hasattr(other, 'model') and self.model is other.model:
            return True
        return False

    def clone_has_same_weights(self, clone: 'ModelPatcher'):
        if not self.is_clone(clone):
            return False

        if len(self.hook_patches) > 0:  # TODO: check if this workaround is necessary
            return False
        if self.current_hooks != clone.current_hooks:
            return False
        if self.forced_hooks != clone.forced_hooks:
            return False
        if self.hook_patches.keys() != clone.hook_patches.keys():
            return False

        if len(self.patches) == 0 and len(clone.patches) == 0:
            return True

        if self.patches_uuid == clone.patches_uuid:
            if len(self.patches) != len(clone.patches):
                logging.warning("WARNING: something went wrong, same patch uuid but different length of patches.")
            else:
                return True

    def memory_required(self, input_shape):
        return self.model.memory_required(input_shape=input_shape)

    def set_model_sampler_cfg_function(self, sampler_cfg_function, disable_cfg1_optimization=False):
        if len(inspect.signature(sampler_cfg_function).parameters) == 3:
            self.model_options["sampler_cfg_function"] = lambda args: sampler_cfg_function(args["cond"], args["uncond"], args["cond_scale"]) #Old way
        else:
            self.model_options["sampler_cfg_function"] = sampler_cfg_function
        if disable_cfg1_optimization:
            self.model_options["disable_cfg1_optimization"] = True

    def set_model_sampler_post_cfg_function(self, post_cfg_function, disable_cfg1_optimization=False):
        self.model_options = set_model_options_post_cfg_function(self.model_options, post_cfg_function, disable_cfg1_optimization)

    def set_model_sampler_pre_cfg_function(self, pre_cfg_function, disable_cfg1_optimization=False):
        self.model_options = set_model_options_pre_cfg_function(self.model_options, pre_cfg_function, disable_cfg1_optimization)

    def set_model_unet_function_wrapper(self, unet_wrapper_function: UnetWrapperFunction):
        self.model_options["model_function_wrapper"] = unet_wrapper_function

    def set_model_denoise_mask_function(self, denoise_mask_function):
        self.model_options["denoise_mask_function"] = denoise_mask_function

    def set_model_patch(self, patch, name):
        to = self.model_options["transformer_options"]
        if "patches" not in to:
            to["patches"] = {}
        to["patches"][name] = to["patches"].get(name, []) + [patch]

    def set_model_patch_replace(self, patch, name, block_name, number, transformer_index=None):
        self.model_options = set_model_options_patch_replace(self.model_options, patch, name, block_name, number, transformer_index=transformer_index)

    def set_model_attn1_patch(self, patch):
        self.set_model_patch(patch, "attn1_patch")

    def set_model_attn2_patch(self, patch):
        self.set_model_patch(patch, "attn2_patch")

    def set_model_attn1_replace(self, patch, block_name, number, transformer_index=None):
        self.set_model_patch_replace(patch, "attn1", block_name, number, transformer_index)

    def set_model_attn2_replace(self, patch, block_name, number, transformer_index=None):
        self.set_model_patch_replace(patch, "attn2", block_name, number, transformer_index)

    def set_model_attn1_output_patch(self, patch):
        self.set_model_patch(patch, "attn1_output_patch")

    def set_model_attn2_output_patch(self, patch):
        self.set_model_patch(patch, "attn2_output_patch")

    def set_model_input_block_patch(self, patch):
        self.set_model_patch(patch, "input_block_patch")

    def set_model_input_block_patch_after_skip(self, patch):
        self.set_model_patch(patch, "input_block_patch_after_skip")

    def set_model_output_block_patch(self, patch):
        self.set_model_patch(patch, "output_block_patch")

    def add_object_patch(self, name, obj):
        self.object_patches[name] = obj

    def get_model_object(self, name):
        if name in self.object_patches:
            return self.object_patches[name]
        else:
            if name in self.object_patches_backup:
                return self.object_patches_backup[name]
            else:
                return comfy.utils.get_attr(self.model, name)

    def model_patches_to(self, device):
        to = self.model_options["transformer_options"]
        if "patches" in to:
            patches = to["patches"]
            for name in patches:
                patch_list = patches[name]
                for i in range(len(patch_list)):
                    if hasattr(patch_list[i], "to"):
                        patch_list[i] = patch_list[i].to(device)
        if "patches_replace" in to:
            patches = to["patches_replace"]
            for name in patches:
                patch_list = patches[name]
                for k in patch_list:
                    if hasattr(patch_list[k], "to"):
                        patch_list[k] = patch_list[k].to(device)
        if "model_function_wrapper" in self.model_options:
            wrap_func = self.model_options["model_function_wrapper"]
            if hasattr(wrap_func, "to"):
                self.model_options["model_function_wrapper"] = wrap_func.to(device)

    def model_dtype(self):
        if hasattr(self.model, "get_dtype"):
            return self.model.get_dtype()

    def add_patches(self, patches, strength_patch=1.0, strength_model=1.0):
        p = set()
        model_sd = self.model.state_dict()
        for k in patches:
            offset = None
            function = None
            if isinstance(k, str):
                key = k
            else:
                offset = k[1]
                key = k[0]
                if len(k) > 2:
                    function = k[2]

            if key in model_sd:
                p.add(k)
                current_patches = self.patches.get(key, [])
                current_patches.append((strength_patch, patches[k], strength_model, offset, function))
                self.patches[key] = current_patches

        self.patches_uuid = uuid.uuid4()
        return list(p)

    def get_key_patches(self, filter_prefix=None):
        model_sd = self.model_state_dict()
        p = {}
        for k in model_sd:
            if filter_prefix is not None:
                if not k.startswith(filter_prefix):
                    continue
            bk = self.backup.get(k, None)
            hbk = self.hook_backup.get(k, None)
            if bk is not None:
                weight = bk.weight
            if hbk is not None:
                weight = hbk[0]
            else:
                weight = model_sd[k]
            if k in self.patches:
                p[k] = [weight] + self.patches[k]
            else:
                p[k] = (weight,)
        return p

    def model_state_dict(self, filter_prefix=None):
        sd = self.model.state_dict()
        keys = list(sd.keys())
        if filter_prefix is not None:
            for k in keys:
                if not k.startswith(filter_prefix):
                    sd.pop(k)
        return sd

    def patch_weight_to_device(self, key, device_to=None, inplace_update=False):
        if key not in self.patches:
            return

        weight = comfy.utils.get_attr(self.model, key)

        inplace_update = self.weight_inplace_update or inplace_update

        if key not in self.backup:
            self.backup[key] = collections.namedtuple('Dimension', ['weight', 'inplace_update'])(weight.to(device=self.offload_device, copy=inplace_update), inplace_update)

        if device_to is not None:
            temp_weight = comfy.model_management.cast_to_device(weight, device_to, torch.float32, copy=True)
        else:
            temp_weight = weight.to(torch.float32, copy=True)
        out_weight = comfy.lora.calculate_weight(self.patches[key], temp_weight, key)
        out_weight = comfy.float.stochastic_rounding(out_weight, weight.dtype, seed=string_to_seed(key))
        if inplace_update:
            comfy.utils.copy_to_param(self.model, key, out_weight)
        else:
            comfy.utils.set_attr_param(self.model, key, out_weight)

    def load(self, device_to=None, lowvram_model_memory=0, force_patch_weights=False, full_load=False):
        self.unpatch_hooks()
        mem_counter = 0
        patch_counter = 0
        lowvram_counter = 0
        loading = []
        for n, m in self.model.named_modules():
            if hasattr(m, "comfy_cast_weights") or hasattr(m, "weight"):
                loading.append((comfy.model_management.module_size(m), n, m))

        load_completely = []
        loading.sort(reverse=True)
        for x in loading:
            n = x[1]
            m = x[2]
            module_mem = x[0]

            lowvram_weight = False

            if not full_load and hasattr(m, "comfy_cast_weights"):
                if mem_counter + module_mem >= lowvram_model_memory:
                    lowvram_weight = True
                    lowvram_counter += 1
                    if hasattr(m, "prev_comfy_cast_weights"): #Already lowvramed
                        continue

            weight_key = "{}.weight".format(n)
            bias_key = "{}.bias".format(n)

            if lowvram_weight:
                if weight_key in self.patches:
                    if force_patch_weights:
                        self.patch_weight_to_device(weight_key)
                    else:
                        m.weight_function = LowVramPatch(weight_key, self.patches)
                        patch_counter += 1
                if bias_key in self.patches:
                    if force_patch_weights:
                        self.patch_weight_to_device(bias_key)
                    else:
                        m.bias_function = LowVramPatch(bias_key, self.patches)
                        patch_counter += 1

                m.prev_comfy_cast_weights = m.comfy_cast_weights
                m.comfy_cast_weights = True
            else:
                if hasattr(m, "comfy_cast_weights"):
                    if m.comfy_cast_weights:
                        wipe_lowvram_weight(m)

                if hasattr(m, "weight"):
                    mem_counter += module_mem
                    load_completely.append((module_mem, n, m))

        load_completely.sort(reverse=True)
        for x in load_completely:
            n = x[1]
            m = x[2]
            weight_key = "{}.weight".format(n)
            bias_key = "{}.bias".format(n)
            if hasattr(m, "comfy_patched_weights"):
                if m.comfy_patched_weights == True:
                    continue

            self.patch_weight_to_device(weight_key, device_to=device_to)
            self.patch_weight_to_device(bias_key, device_to=device_to)
            logging.debug("lowvram: loaded module regularly {} {}".format(n, m))
            m.comfy_patched_weights = True

        for x in load_completely:
            x[2].to(device_to)

        if lowvram_counter > 0:
            logging.info("loaded partially {} {} {}".format(lowvram_model_memory / (1024 * 1024), mem_counter / (1024 * 1024), patch_counter))
            self.model.model_lowvram = True
        else:
            logging.info("loaded completely {} {} {}".format(lowvram_model_memory / (1024 * 1024), mem_counter / (1024 * 1024), full_load))
            self.model.model_lowvram = False
            if full_load:
                self.model.to(device_to)
                mem_counter = self.model_size()

        self.model.lowvram_patch_counter += patch_counter
        self.model.device = device_to
        self.model.model_loaded_weight_memory = mem_counter
        self.apply_hooks(self.forced_hooks)

    def patch_model(self, device_to=None, lowvram_model_memory=0, load_weights=True, force_patch_weights=False):
        for k in self.object_patches:
            old = comfy.utils.set_attr(self.model, k, self.object_patches[k])
            if k not in self.object_patches_backup:
                self.object_patches_backup[k] = old

        if lowvram_model_memory == 0:
            full_load = True
        else:
            full_load = False

        if load_weights:
            self.load(device_to, lowvram_model_memory=lowvram_model_memory, force_patch_weights=force_patch_weights, full_load=full_load)
        return self.model

    def unpatch_model(self, device_to=None, unpatch_weights=True):
        if unpatch_weights:
            self.unpatch_hooks()
            if self.model.model_lowvram:
                for m in self.model.modules():
                    wipe_lowvram_weight(m)

                self.model.model_lowvram = False
                self.model.lowvram_patch_counter = 0

            keys = list(self.backup.keys())

            for k in keys:
                bk = self.backup[k]
                if bk.inplace_update:
                    comfy.utils.copy_to_param(self.model, k, bk.weight)
                else:
                    comfy.utils.set_attr_param(self.model, k, bk.weight)

            self.backup.clear()

            if device_to is not None:
                self.model.to(device_to)
                self.model.device = device_to
            self.model.model_loaded_weight_memory = 0

            for m in self.model.modules():
                if hasattr(m, "comfy_patched_weights"):
                    del m.comfy_patched_weights

        keys = list(self.object_patches_backup.keys())
        for k in keys:
            comfy.utils.set_attr(self.model, k, self.object_patches_backup[k])

        self.object_patches_backup.clear()

    def partially_unload(self, device_to, memory_to_free=0):
        memory_freed = 0
        patch_counter = 0
        unload_list = []

        for n, m in self.model.named_modules():
            shift_lowvram = False
            if hasattr(m, "comfy_cast_weights"):
                module_mem = comfy.model_management.module_size(m)
                unload_list.append((module_mem, n, m))

        unload_list.sort()
        for unload in unload_list:
            if memory_to_free < memory_freed:
                break
            module_mem = unload[0]
            n = unload[1]
            m = unload[2]
            weight_key = "{}.weight".format(n)
            bias_key = "{}.bias".format(n)

            if hasattr(m, "comfy_patched_weights") and m.comfy_patched_weights == True:
                for key in [weight_key, bias_key]:
                    bk = self.backup.get(key, None)
                    if bk is not None:
                        if bk.inplace_update:
                            comfy.utils.copy_to_param(self.model, key, bk.weight)
                        else:
                            comfy.utils.set_attr_param(self.model, key, bk.weight)
                        self.backup.pop(key)

                m.to(device_to)
                if weight_key in self.patches:
                    m.weight_function = LowVramPatch(weight_key, self.patches)
                    patch_counter += 1
                if bias_key in self.patches:
                    m.bias_function = LowVramPatch(bias_key, self.patches)
                    patch_counter += 1

                m.prev_comfy_cast_weights = m.comfy_cast_weights
                m.comfy_cast_weights = True
                m.comfy_patched_weights = False
                memory_freed += module_mem
                logging.debug("freed {}".format(n))

        self.model.model_lowvram = True
        self.model.lowvram_patch_counter += patch_counter
        self.model.model_loaded_weight_memory -= memory_freed
        return memory_freed

    def partially_load(self, device_to, extra_memory=0):
        self.unpatch_model(unpatch_weights=False)
        self.patch_model(load_weights=False)
        full_load = False
        if self.model.model_lowvram == False:
            return 0
        if self.model.model_loaded_weight_memory + extra_memory > self.model_size():
            full_load = True
        current_used = self.model.model_loaded_weight_memory
        self.load(device_to, lowvram_model_memory=current_used + extra_memory, full_load=full_load)
        return self.model.model_loaded_weight_memory - current_used

    def current_loaded_device(self):
        return self.model.device

    def calculate_weight(self, patches, weight, key, intermediate_dtype=torch.float32):
        print("WARNING the ModelPatcher.calculate_weight function is deprecated, please use: comfy.lora.calculate_weight instead")
        return comfy.lora.calculate_weight(patches, weight, key, intermediate_dtype=intermediate_dtype)

    def cleanup(self):
        self.clean_hooks()
        self.restore_hook_patches()
        for callback in self.callbacks[CallbacksMP.ON_CLEANUP]:
            callback(self)

    def add_callback(self, key, callback: Callable):
        if key not in self.callbacks:
            raise Exception(f"Callback '{key}' is not recognized.")
        self.callbacks[key].append(callback)
    
    def add_attachment(self, attachment):
        self.attachments.append(attachment)

    def pre_run(self):
        for callback in self.callbacks[CallbacksMP.ON_PRE_RUN]:
            callback(self)
    
    def prepare_state(self, timestep):
        for callback in self.callbacks[CallbacksMP.ON_PREPARE_STATE]:
            callback(self, timestep)

    def restore_hook_patches(self):
        if len(self.hook_patches_backup) > 0:
            self.hook_patches = self.hook_patches_backup
            self.hook_patches_backup = {}

    def set_hook_mode(self, hook_mode: comfy.hooks.EnumHookMode):
        self.hook_mode = hook_mode
    
    def prepare_hook_patches_current_keyframe(self, t: torch.Tensor, hook_group: comfy.hooks.HookGroup):
        curr_t = t[0]
        for hook in hook_group.hooks:
            changed = hook.hook_keyframe.prepare_current_keyframe(curr_t=curr_t)
            # if keyframe changed, remove any cached HookGroups that contain hook with the same hook_ref;
            # this will cause the weights to be recalculated when sampling
            if changed:
                # reset current_hooks if contains hook that changed
                if self.current_hooks is not None:
                    for current_hook in self.current_hooks.hooks:
                        if current_hook == hook:
                            self.current_hooks = None
                            break
                for cached_group in list(self.cached_hook_patches.keys()):
                    if cached_group.contains(hook):
                        self.cached_hook_patches.pop(cached_group)

    def register_all_hook_patches(self, hooks_dict: Dict[comfy.hooks.Hook, None], target: comfy.hooks.EnumWeightTarget):
        self.restore_hook_patches()
        weight_hooks_to_register: List[comfy.hooks.WeightHook] = []
        for hook in hooks_dict:
            if hook.hook_type == comfy.hooks.EnumHookType.Weight:
                if hook.hook_ref not in self.hook_patches:
                    weight_hooks_to_register.append(hook)
        if len(weight_hooks_to_register) > 0:
            self.hook_patches_backup = create_hook_patches_clone(self.hook_patches)
            for hook in weight_hooks_to_register:
                hook.add_hook_patches(self, target)
        for callback in self.callbacks[CallbacksMP.ON_REGISTER_ALL_HOOK_PATCHES]:
            callback(self, hooks_dict, target)

    def add_hook_patches(self, hook: comfy.hooks.WeightHook, patches, strength_patch=1.0, strength_model=1.0, is_diff=False):
        # NOTE: this mirrors behavior of add_patches func
        if is_diff:
            comfy.model_management.unload_model_clones(self)
        current_hook_patches: Dict[str,List] = self.hook_patches.get(hook.hook_ref, {})
        p = set()
        model_sd = self.model.state_dict()
        for k in patches:
            offset = None
            function = None
            if isinstance(k, str):
                key = k
            else:
                offset = k[1]
                key = k[0]
                if len(k) > 2:
                    function = k[2]
            
            if key in model_sd:
                p.add(k)
                current_patches: List[Tuple] = current_hook_patches.get(key, [])
                if is_diff:
                    # take difference between desired weight and existing weight to get diff
                    # TODO: try to implement diff via strength_path/strength_model diff
                    model_dtype = comfy.utils.get_attr(self.model, key).dtype
                    if model_dtype in [torch.float8_e5m2, torch.float8_e4m3fn]:
                        diff_weight = (patches[k].to(torch.float32)-comfy.utils.get_attr(self.model, key).to(torch.float32)).to(model_dtype)
                    else:
                        diff_weight = patches[k]-comfy.utils.get_attr(self.model, key)
                    current_patches.append((strength_patch, (diff_weight,), strength_model, offset, function))
                else:
                    current_patches.append((strength_patch, patches[k], strength_model, offset, function))
                current_hook_patches[key] = current_patches
        self.hook_patches[hook.hook_ref] = current_hook_patches
        # since should care about these patches too to determine if same model, reroll patches_uuid
        self.patches_uuid = uuid.uuid4()
        return list(p)

    def get_weight_diffs(self, patches):
        comfy.model_management.unload_model_clones(self)
        weights: Dict[str, Tuple] = {}
        p = set()
        model_sd = self.model.state_dict()
        for k in patches:
            if k in model_sd:
                p.add(k)
                model_dtype = comfy.utils.get_attr(self.model, k).dtype
                if model_dtype in [torch.float8_e5m2, torch.float8_e4m3fn]:
                    diff_weight = (patches[k].to(torch.float32)-comfy.utils.get_attr(self.model, k).to(torch.float32)).to(model_dtype)
                else:
                    diff_weight = patches[k]-comfy.utils.get_attr(self.model, k)
                weights[k] = (diff_weight,)
        return weights, p

    def get_combined_hook_patches(self, hooks: comfy.hooks.HookGroup):
        # combined_patches will contain  weights of all relevant hooks, per key
        combined_patches = {}
        if hooks is not None:
            for hook in hooks.hooks:
                hook_patches: Dict = self.hook_patches.get(hook.hook_ref, {})
                for key in hook_patches.keys():
                    current_patches: List[Tuple] = combined_patches.get(key, [])
                    if math.isclose(hook.strength, 1.0):
                        current_patches.extend(hook_patches[key])
                    else:
                        # patches are stored as tuples: (strength_patch, (tuple_with_weights,), strength_model)
                        for patch in hook_patches[key]:
                            new_patch = list(patch)
                            new_patch[0] *= hook.strength
                            current_patches.append(tuple(new_patch))
                    combined_patches[key] = current_patches
        return combined_patches

    def apply_hooks(self, hooks: comfy.hooks.HookGroup):
        if self.current_hooks == hooks:
            return
        self.patch_hooks(hooks=hooks)
        for callback in self.callbacks[CallbacksMP.ON_APPLY_HOOKS]:
            callback(self, hooks)

    def patch_hooks(self, hooks: comfy.hooks.HookGroup):
        self.unpatch_hooks()
        model_sd = self.model_state_dict()
        # if have cached weights for hooks, use it
        cached_weights = self.cached_hook_patches.get(hooks, None)
        if cached_weights is not None:
            for key in cached_weights:
                if key not in model_sd:
                    print(f"WARNING cached hook could not patch. key does not exist in model: {key}")
                    continue
                self.patch_cached_hook_weights(cached_weights=cached_weights, key=key)
        else:
            relevant_patches = self.get_combined_hook_patches(hooks=hooks)
            original_weights = None
            if len(relevant_patches) > 0:
                original_weights = self.get_key_patches()
            for key in relevant_patches:
                if key not in model_sd:
                    print(f"WARNING cached hook would not patch. key does not exist in model: {key}")
                    continue
                self.patch_hook_weight_to_device(hooks=hooks, combined_patches=relevant_patches, key=key, original_weights=original_weights)
        self.current_hooks = hooks

    def patch_cached_hook_weights(self, cached_weights: Dict, key: str):
        if key not in self.hook_backup:
            weight: torch.Tensor = comfy.utils.get_attr(self.model, key)
            target_device = self.offload_device
            if self.hook_mode == comfy.hooks.EnumHookMode.MaxSpeed:
                target_device = weight.device
            self.hook_backup[key] = (weight.to(device=target_device, copy=self.weight_inplace_update), weight.device)
        if self.weight_inplace_update:
            comfy.utils.copy_to_param(self.model, key, cached_weights[key])
        else:
            comfy.utils.set_attr_param(self.model, key, cached_weights[key])

    def clear_cached_hook_weights(self):
        self.cached_hook_patches.clear()
        self.current_hooks = None

    def patch_hook_weight_to_device(self, hooks: comfy.hooks.HookGroup, combined_patches: dict, key: str, original_weights: dict):
        if key not in combined_patches:
            return
        weight: torch.Tensor = comfy.utils.get_attr(self.model, key)
        if key not in self.hook_backup:
            target_device = self.offload_device
            if self.hook_mode == comfy.hooks.EnumHookMode.MaxSpeed:
                target_device = weight.device
            self.hook_backup[key] = (weight.to(device=target_device, copy=self.weight_inplace_update), weight.device)
        
        # TODO: properly handle lowvram situations for cached hook patches
        temp_weight = comfy.model_management.cast_to_device(weight, weight.device, torch.float32, copy=True)
        out_weight = comfy.lora.calculate_weight(combined_patches[key], temp_weight, key, original_weights=original_weights).to(weight.dtype)
        if self.hook_mode == comfy.hooks.EnumHookMode.MaxSpeed:
            self.cached_hook_patches.setdefault(hooks, {})
            self.cached_hook_patches[hooks][key] = out_weight
        if self.weight_inplace_update:
            comfy.utils.copy_to_param(self.model, key, out_weight)
        else:
            comfy.utils.set_attr_param(self.model, key, out_weight)
    
    def unpatch_hooks(self) -> None:
        if len(self.hook_backup) == 0:
            self.current_hooks = None
            return
        keys = list(self.hook_backup.keys())
        if self.weight_inplace_update:
            for k in keys:
                if self.hook_mode == comfy.hooks.EnumHookMode.MaxSpeed: # does not need to be cast; device already matches
                    comfy.utils.copy_to_param(self.model, k, self.hook_backup[k][0])
                else:
                    comfy.utils.copy_to_param(self.model, k, self.hook_backup[k][0].to(device=self.hook_backup[k][1]))
        else:
            for k in keys:
                if self.hook_mode == comfy.hooks.EnumHookMode.MaxSpeed:
                    comfy.utils.set_attr_param(self.model, k, self.hook_backup[k][0])
                else:
                    comfy.utils.set_attr_param(self.model, k, self.hook_backup[k][0].to(device=self.hook_backup[k][1]))
                
        self.hook_backup.clear()
        self.current_hooks = None

    def clean_hooks(self):
        self.unpatch_hooks()
        self.clear_cached_hook_weights()
