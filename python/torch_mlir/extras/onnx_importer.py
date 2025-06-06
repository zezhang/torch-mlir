# Based on code Copyright (c) Advanced Micro Devices, Inc.
#
# Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
# See https://llvm.org/LICENSE.txt for license information.
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
# Also available under a BSD-style license. See LICENSE.

"""Imports ONNX graphs to `torch` dialect ops.

See documentation:
    https://github.com/llvm/torch-mlir/blob/main/docs/importers/onnx_importer.md

This file is distributed/forked verbatim into various downstream projects, and
it must abide by several rules above and beyond the rest of the codebase:
    - It must be standalone, only depending on:
        - `onnx`
        - `..ir` relative imports to the main IR directory
        - `..dialects.func` relative import to the `func` dialect (TODO:
           we are looking to eliminate this dep).
        - Python standard library
    - It does not directly use the ODS generated `torch` dialect Python
      wrappers. This allows it to be used in contexts that only build a C++
      compiler with minimal IR Python bindings.
    - It is intended as an enabler for full onnx compilation, only handling
      the import from ONNX -> the `torch` dialect. Testing, full pipelines,
      and utilities belong elsewhere.
"""

try:
    import onnx
except ModuleNotFoundError as e:
    raise ModuleNotFoundError(
        "The onnx package (`pip install onnx`) is required to use the onnx importer"
    ) from e

from typing import Optional, List, Dict, Tuple
import warnings

from dataclasses import dataclass, field

import numpy as np
import re

from ..ir import (
    ArrayAttr,
    Attribute,
    Block,
    Context,
    DenseElementsAttr,
    DenseResourceElementsAttr,
    DictAttr,
    FloatAttr,
    BF16Type,
    ComplexType,
    F16Type,
    F32Type,
    F64Type,
    Float8E4M3FNUZType,
    Float8E4M3FNType,
    Float8E5M2FNUZType,
    Float8E5M2Type,
    FunctionType,
    InsertionPoint,
    IntegerAttr,
    IntegerType,
    MLIRError,
    RankedTensorType,
    Location,
    Module,
    Operation,
    StringAttr,
    Type as IrType,
    Value,
)

from ..dialects import (
    func as func_dialect,
)


@dataclass
class Config:
    """Various configuration settings for the importer."""

    # Ancient ONNX exporters would often add a model input for anything that
    # might be mutable, providing an initializer for it as well. More modern
    # tools tools realized this is a really bad idea for a lot of reasons.
    # We choose to assume more recent norms, even if encountering older
    # models. Setting this to False probably won't do what you want but
    # should produce interesting errors to waste your time deciphering.
    # We mainly use it as a way to document in the code that we are
    # making an assumption.
    elide_initialized_inputs: bool = True

    # Some ONNX operators are defined by ONNX functions and will be
    # automatically expanded (see get_operator_function() below) to MLIR
    # functions by the importer. This option allows allowlisting functions that
    # should be expanded. If this is None, then allowlisting is not used (all
    # functions not explicitly denylisted will be expanded).
    #
    # Since function expansion has not always been supported, the default should
    # be to use allowlisting, to avoid disruption.
    function_expansion_allowlists_by_domain: Optional[Dict[str, set[str]]] = field(
        default_factory=lambda: {
            # Default domain (ONNX built-in ops)
            "": {
                "MeanVarianceNormalization",
            }
        }
    )

    # Some ONNX operators are defined by ONNX functions and will be
    # automatically expanded (see get_operator_function() below) to MLIR
    # functions by the importer. This option allows denylisting functions that
    # should not be expanded.
    function_expansion_denylists_by_domain: Dict[str, set[str]] = field(
        default_factory=lambda: {
            # Default domain (ONNX built-in ops)
            "": {
                # CastLike's second input `target_type` is used only for its
                # type (T2), from which its output's type is inferred, but
                # because its value is unused, ONNX's shape inference doesn't
                # annotate the input value with a type, so looking up the
                # function by the provided input types will fail.
                "CastLike",
                # ONNX errors when trying to infer the type of the Loop op
                # within this function: "[ShapeInferenceError] Inferred shape
                # and existing shape differ in rank: (1) vs (0)"
                "Range",
            }
        }
    )


class ModelInfo:
    """Top-level accounting and accessors for an ONNX model."""

    def __init__(self, model_proto: onnx.ModelProto, *, config: Config = Config()):
        self.config = config
        self.model_proto = model_proto
        assert model_proto.graph, "Model must contain a main Graph"
        self.main_graph = GraphInfo(self, model_proto.graph)

    def create_module(self, context: Optional[Context] = None) -> Module:
        if not context:
            context = Context()
        module = Module.create(Location.unknown(context))
        # TODO: Populate module level metadata from the ModelProto
        return module


class GraphInfo:
    """Information about a Graph within a model."""

    def __init__(
        self,
        model_info: ModelInfo,
        graph_proto: onnx.GraphProto,
        is_subgraph: bool = False,
    ):
        self.model_info = model_info
        self.graph_proto = graph_proto
        self.initializer_map: Dict[str, onnx.TensorProto] = {
            n.name: n for n in graph_proto.initializer
        }
        self.value_info_map: Dict[str, onnx.ValueInfoProto] = {
            n.name: n for n in graph_proto.value_info
        }
        self.declared_input_map: Dict[str, onnx.ValueInfoProto] = {
            n.name: n for n in graph_proto.input
        }
        self.output_map: Dict[str, onnx.ValueInfoProto] = {
            n.name: n for n in graph_proto.output
        }

        # Generate the effective input map, which for old models can be a
        # subset of the input map.
        if (
            not is_subgraph
            and model_info
            and model_info.config.elide_initialized_inputs
        ):
            self.input_map = {
                k: v
                for k, v in self.declared_input_map.items()
                if k not in self.initializer_map
            }
        else:
            self.input_map = self.declared_input_map
            illegal_input_keys = self.input_map.keys() - (
                self.input_map.keys() - self.initializer_map.keys()
            )
            assert self.input_map.keys().isdisjoint(self.initializer_map.keys()), (
                f"When not in elide_initialized_inputs=True, we expect inputs to not "
                f"have an initial value (got {illegal_input_keys})."
            )

    def find_type_proto_for_name(self, name: str) -> onnx.TypeProto:
        # Node outputs don't typically have type information, but shape inference
        # will associate them in the value_info. If not there, it may be a
        # graph output, which must have type information.
        value_info = (
            self.value_info_map.get(name)
            or self.output_map.get(name)
            or self.declared_input_map.get(name)
        )
        if value_info is not None:
            return value_info.type

        tensor_proto = self.initializer_map.get(name)
        if tensor_proto is not None:
            return onnx.helper.make_tensor_type_proto(
                tensor_proto.data_type, tensor_proto.dims
            )

        # No type information is associated, this can occur when the value is unused:
        return ""


class OnnxImportError(Exception): ...


class NodeImporter:
    """Imports graph nodes into MLIR.

    Typically, the top level graph will be imported into a func whereas dependent
    graphs may just be imported with references to pre-existing values.

    Note that ONNX requires that graphs be sorted topologically and free of cycles,
    so we don't take any special steps to order them for dominance.
    """

    __slots__ = [
        "_c",
        "_cc",
        "_m",
        "_mc",
        "_gi",
        "_p",
        "_b",
        "_nv_map",
    ]

    def __init__(
        self,
        graph_info: GraphInfo,
        *,
        parent_op: Operation,
        block: Block,
        context_cache: "ContextCache",
        module_op: Operation,
        module_cache: "ModuleCache",
    ):
        self._c = parent_op.context
        self._cc = context_cache
        self._m = module_op
        self._mc = module_cache
        self._gi = graph_info
        self._p = parent_op
        self._b = block
        self._nv_map: Dict[str, Value] = {}

    @classmethod
    def define_function(
        cls,
        graph_info: GraphInfo,
        module_op: Operation,
        context_cache: Optional["ContextCache"] = None,
        module_cache: Optional["ModuleCache"] = None,
        private: bool = False,
    ) -> "NodeImporter":
        cc = (
            context_cache
            if context_cache is not None
            else ContextCache(module_op.context)
        )
        mc = module_cache if module_cache is not None else ModuleCache(module_op, cc)
        with module_op.context, Location.name(f"graph:{graph_info.graph_proto.name}"):
            body = module_op.regions[0].blocks[0]
            func_name = graph_info.graph_proto.name
            input_types = [
                cc.type_proto_to_type(inp.type) for inp in graph_info.input_map.values()
            ]
            output_types = [
                cc.type_proto_to_type(out.type)
                for out in graph_info.output_map.values()
            ]
            ftype = FunctionType.get(input_types, output_types)
            func_op = func_dialect.FuncOp(
                func_name,
                ftype,
                ip=InsertionPoint(body),
                visibility="private" if private else None,
            )
            block = func_op.add_entry_block(
                [Location.name(k) for k in graph_info.input_map.keys()]
            )
        imp = NodeImporter(
            graph_info,
            parent_op=func_op,
            block=block,
            context_cache=cc,
            module_op=module_op,
            module_cache=mc,
        )
        for node_name, input_value in zip(graph_info.input_map.keys(), block.arguments):
            imp._nv_map[node_name] = input_value
        imp._populate_graph_attrs(func_op)
        return imp

    def _populate_graph_attrs(self, container_op: Operation):
        """Populates graph level meta attributes on the given container op."""
        m = self._gi.model_info.model_proto
        with container_op.context:
            i64_type = IntegerType.get_signed(64)
            default_opset_version = 0
            opset_versions: Dict[str, IntegerAttr] = {}
            for opset_import in m.opset_import:
                if opset_import.domain:
                    opset_versions[opset_import.domain] = IntegerAttr.get(
                        i64_type, opset_import.version
                    )
                else:
                    default_opset_version = opset_import.version
            if default_opset_version:
                container_op.attributes["torch.onnx_meta.opset_version"] = (
                    IntegerAttr.get(i64_type, default_opset_version)
                )
            if opset_versions:
                container_op.attributes["torch.onnx_meta.opset_versions"] = (
                    DictAttr.get(opset_versions)
                )
            container_op.attributes["torch.onnx_meta.ir_version"] = IntegerAttr.get(
                IntegerType.get_signed(64), m.ir_version
            )
            container_op.attributes["torch.onnx_meta.producer_name"] = StringAttr.get(
                m.producer_name
            )
            container_op.attributes["torch.onnx_meta.producer_version"] = (
                StringAttr.get(m.producer_version)
            )

    def import_all(self, func=True):
        """Imports all nodes topologically."""
        # TODO: Consider pulling in initializers on demand since there can be so
        # much unused crap.
        for init in self._gi.initializer_map.values():
            self.import_initializer(init)

        self.get_none()
        for node in self._gi.graph_proto.node:
            self.import_node(node)

        outputs = []
        for output_name in self._gi.output_map.keys():
            try:
                outputs.append(self._nv_map[output_name])
            except KeyError:
                raise OnnxImportError(
                    f"Non topologically produced ONNX graph output '{output_name}'"
                )
        with InsertionPoint(self._b), Location.unknown():
            if func:
                func_dialect.ReturnOp(outputs)
            else:
                Operation.create(name="torch.operator_terminator", operands=outputs)

    def get_none(self):
        if "" in self._nv_map:
            return self._nv_map[""]

        with InsertionPoint(self._b), Location.name("onnx_importer.none"):
            nne = Operation.create(
                name="torch.constant.none",
                results=[self._cc.get_none_type()],
                operands=[],
                attributes={},
            ).results[0]
            self._nv_map[""] = nne
            return nne

    def import_node(self, node: onnx.NodeProto):
        with InsertionPoint(self._b), Location.name(node.name):
            op_type = node.op_type
            op_domain = node.domain

            # Handle special op types that materialize to non-op IR constructs.
            # Handlers return True if the op was handled, else this function
            # should process it as a general node.
            special_key = f"_handle_node_{op_type}"
            if hasattr(self, special_key):
                was_handled = getattr(self, special_key)(node)
                if was_handled:
                    return
            # General node import.
            input_values = []
            input_type_protos = []
            for input_name in node.input:
                try:
                    input_values.append(self._nv_map[input_name])
                    # Missing optional arguments will have empty types
                    input_type_protos.append(
                        self._gi.find_type_proto_for_name(input_name)
                        or onnx.TypeProto()
                    )
                except KeyError:
                    raise OnnxImportError(
                        f"Non topologically produced ONNX node input '{input_name}': {node}"
                    )

            output_names = []
            output_type_protos = []
            output_types = []
            for output_name in node.output:
                output_names.append(output_name)
                type_proto = self._gi.find_type_proto_for_name(output_name)
                output_type_protos.append(type_proto)
                output_types.append(self._cc.type_proto_to_type(type_proto))

            for opset_import in self._gi.model_info.model_proto.opset_import:
                if opset_import.domain == op_domain:
                    opset_version = opset_import.version
                    break
            operator_func_op = self._mc.get_operator_function(
                op_type,
                op_domain,
                opset_version,
                input_type_protos,
                output_type_protos,
                node,
                self._gi.model_info.config,
            )

            if operator_func_op is not None:
                custom_op = func_dialect.CallOp(operator_func_op, input_values)
            else:
                attrs = self.import_attributes(node.attribute)
                attrs["name"] = StringAttr.get(f"onnx.{op_type}")
                regions = self.count_regions(node.attribute)
                custom_op = Operation.create(
                    name="torch.operator",
                    results=output_types,
                    operands=input_values,
                    attributes=attrs,
                    regions=regions,
                )
                self.import_regions(node.attribute, custom_op)

            for output_name, output_value in zip(output_names, custom_op.results):
                self._nv_map[output_name] = output_value

    def import_attributes(self, onnx_attrs: List[onnx.AttributeProto]):
        attrs = {}
        for onnx_attr in onnx_attrs:
            attr_type = onnx_attr.type
            if attr_type not in ATTRIBUTE_TYPE_HANDLERS:
                raise OnnxImportError(
                    f"Unhandled ONNX attribute type code {attr_type}: {onnx_attr}"
                )
            handler = ATTRIBUTE_TYPE_HANDLERS[attr_type]
            if handler is None:
                # Active skip.
                continue
            elif handler is False:
                # Active error.
                # try matching attribute type ID to name for a more descriptive error message
                try:
                    attr_type_name = onnx.AttributeProto.AttributeType.Name(attr_type)
                except ValueError:
                    attr_type_name = "UNKNOWN"
                raise OnnxImportError(
                    f"ONNX importer does not support generic node attribute type {attr_type_name} "
                    f"with ID {attr_type}. "
                    f"This likely means that this is a special node which requires specific "
                    f"handling in the importer: {onnx_attr}"
                )
            result = handler(onnx_attr, self._cc)
            attrs[f"torch.onnx.{onnx_attr.name}"] = result
        return attrs

    def count_regions(self, onnx_attrs: List[onnx.AttributeProto]):
        count = 0
        for onnx_attr in onnx_attrs:
            if onnx_attr.type == onnx.AttributeProto.AttributeType.GRAPH:
                count += 1
        return count

    def import_regions(self, onnx_attrs: List[onnx.AttributeProto], op):
        attr_map = {}
        for onnx_attr in onnx_attrs:
            attr_type = onnx_attr.type
            if attr_type != onnx.AttributeProto.AttributeType.GRAPH:
                continue
            attr_map[onnx_attr.name] = onnx_attr

        for name, region in zip(sorted(attr_map.keys()), op.regions):
            attr = attr_map[name]
            block_types = [
                self._cc.type_proto_to_type(input.type) for input in attr.g.input
            ]
            block_names = [input.name for input in attr.g.input]
            region.blocks.append(
                *block_types, arg_locs=[op.location] * len(block_types)
            )
            block = region.blocks[0]
            graph_info = GraphInfo(self._gi.model_info, attr.g, is_subgraph=True)
            imp = NodeImporter(
                graph_info,
                parent_op=op,
                block=block,
                context_cache=self._cc,
                module_op=self._m,
                module_cache=self._mc,
            )

            for node_name, input_value in zip(block_names, block.arguments):
                imp._nv_map[node_name] = input_value
            for k in self._nv_map:
                imp._nv_map[k] = self._nv_map[k]

            imp.import_all(False)

    def import_initializer(
        self, initializer: onnx.TensorProto, extern_name: str = None
    ) -> Value:
        # If an explicitly specified name is given, use that; otherwise, pick
        # up the name from the tensor proto itself
        iname = extern_name if extern_name else initializer.name
        with InsertionPoint(self._b), Location.name(iname):
            value_attr = self._cc.tensor_proto_to_attr(initializer)
            vtensor_type = self._cc.tensor_proto_to_type(initializer)
            attrs = {
                "name": StringAttr.get(f"onnx.Constant"),
                "torch.onnx.value": value_attr,
            }
            literal_op = Operation.create(
                name="torch.operator",
                results=[vtensor_type],
                attributes=attrs,
            )
            self._nv_map[iname] = literal_op.result
        return literal_op.result

    def _get_immediate_tensor(self, name: str) -> np.array:
        try:
            initializer = self._gi.initializer_map[name]
        except KeyError:
            raise OnnxImportError(
                f"An immediate value for '{name}' was required but it is dynamically produced."
            )
        try:
            dtype = ELEM_TYPE_TO_NUMPY_DTYPE[initializer.data_type]
        except KeyError:
            raise OnnxImportError(
                f"Unknown ONNX tensor element type to numpy dtype mapping: {initializer.data_type}"
            )
        raw_data = initializer.raw_data
        if raw_data:
            return np.frombuffer(raw_data, dtype=dtype).reshape(tuple(initializer.dims))
        else:
            raise OnnxImportError(
                f"Unhandled ONNX TensorProto immediate data: {initializer}"
            )

    def _handle_node_Constant(self, node: onnx.NodeProto) -> bool:
        # Special case only for constants specified by value attribute (for now)
        value_proto = _get_attr(node, "value", False)
        if not value_proto:
            return False

        # Produce an initializer for the constant, so that it can be used in
        # combination with other ops, such as ConstantOfShape, requiring
        # a constant input
        assert value_proto.type == onnx.AttributeProto.AttributeType.TENSOR
        assert len(node.output) == 1
        const_name = node.output[0]
        self.import_initializer(value_proto.t, const_name)
        self._gi.initializer_map[const_name] = value_proto.t
        return True


class ContextCache:
    """Caches per-context lookups of various things."""

    __slots__ = [
        "_c",
        "_elem_type_map",
        "_list_type_map",
        "_optional_type_map",
        "_vtensor_type_map",
    ]

    def __init__(self, context: Context):
        self._c = context
        self._elem_type_map: Dict[int, IrType] = {}
        self._list_type_map: Dict[str, IrType] = {}
        self._optional_type_map: Dict[str, IrType] = {}
        self._vtensor_type_map: Dict[Tuple[Tuple[Optional[int]], IrType], IrType] = {}

    def tensor_element_type(self, elem_type: int) -> IrType:
        t = self._elem_type_map.get(elem_type)
        if t is None:
            try:
                with self._c:
                    t = ELEM_TYPE_TO_IR_TYPE_CB[elem_type]()
            except KeyError:
                raise OnnxImportError(f"Unknown ONNX tensor element type: {elem_type}")
            self._elem_type_map[elem_type] = t
        return t

    def get_none_type(self):
        return IrType.parse("!torch.none", context=self._c)

    def get_list_type(self, element_type: IrType) -> IrType:
        key = str(element_type)
        t = self._list_type_map.get(key)
        if t is None:
            asm = f"!torch.list<{str(element_type)}>"
            try:
                t = IrType.parse(asm, context=self._c)
            except MLIRError as e:
                raise OnnxImportError(
                    f"Unparseable torch type (MLIR asm format bug?): {asm}"
                ) from e
            self._list_type_map[key] = t
        return t

    def get_optional_type(self, element_type: IrType) -> IrType:
        key = str(element_type)
        t = self._optional_type_map.get(key)
        if t is None:
            asm = f"!torch.optional<{str(element_type)}>"
            try:
                t = IrType.parse(asm, context=self._c)
            except MLIRError as e:
                raise OnnxImportError(
                    f"Unparseable torch type (MLIR asm format bug?): {asm}"
                ) from e
            self._optional_type_map[key] = t
        return t

    def get_list_element_type(self, tp: onnx.TypeProto) -> IrType:
        tt = tp.tensor_type
        if tt.elem_type:
            element_type = self.tensor_element_type(tt.elem_type)
            dims = tuple(
                (d.dim_value if d.HasField("dim_value") else None) for d in tt.shape.dim
            )
            shape_asm = ",".join("?" if d is None else str(d) for d in dims)
            return f"vtensor<[{shape_asm}],{element_type}>"

        raise OnnxImportError(f"Unsupport list element type")

    def get_optional_element_type(self, tp: onnx.TypeProto) -> IrType:
        st = tp.sequence_type
        tt = tp.tensor_type
        if tt.elem_type:
            element_type = self.tensor_element_type(tt.elem_type)
            dims = tuple(
                (d.dim_value if d.HasField("dim_value") else None) for d in tt.shape.dim
            )
            shape_asm = ",".join("?" if d is None else str(d) for d in dims)
            return f"vtensor<[{shape_asm}],{element_type}>"

        if st.elem_type:
            element_type = self.get_list_element_type(st.elem_type)
            return f"list<{element_type}>"

        raise OnnxImportError(f"Unsupport optional element type")

    def get_vtensor_type(
        self, dims: Tuple[Optional[int]], element_type: IrType
    ) -> IrType:
        key = (dims, element_type)
        t = self._vtensor_type_map.get(key)
        if t is None:
            shape_asm = ",".join("?" if d is None else str(d) for d in dims)
            asm = f"!torch.vtensor<[{shape_asm}],{str(element_type)}>"
            try:
                t = IrType.parse(asm, context=self._c)
            except MLIRError as e:
                raise OnnxImportError(
                    f"Unparseable torch type (MLIR asm format bug?): {asm}"
                ) from e
            self._vtensor_type_map[key] = t
        return t

    def tensor_proto_to_type(self, tp: onnx.TensorProto) -> IrType:
        element_type = self.tensor_element_type(tp.data_type)
        return self.get_vtensor_type(tuple(tp.dims), element_type)

    def tensor_proto_to_builtin_type(self, tp: onnx.TensorProto) -> IrType:
        element_type = self.tensor_element_type(tp.data_type)
        # TODO: Fixme upstream: RankedTensorType.get should not require a location.
        with Location.unknown():
            try:
                return RankedTensorType.get(tuple(tp.dims), element_type)
            except TypeError as e:
                raise OnnxImportError(f"Unsupported builtin tensor type") from e

    def type_proto_to_type(self, tp: onnx.TypeProto) -> IrType:
        if tp == "":
            warnings.warn(
                "Found a node without a valid type proto. Consider updating the opset_version of"
                " the model and/or running the importer with the flag '--clear-domain'."
            )
            return self.get_none_type()

        tt = tp.tensor_type
        if tt.elem_type:
            element_type = self.tensor_element_type(tt.elem_type)
            dims = tuple(
                # NOTE: dynamic dimension can either be denoted by d.dim_param being set
                #       (and d.dim_value consequently not set) or
                #       by neither d.dim_value nor d.dim_param being set. Also note that
                #       d.dim_value being 0 corresponds to the protobuf default when the field
                #       is not set.
                d.dim_value if d.HasField("dim_value") else None
                for d in tt.shape.dim
            )
            return self.get_vtensor_type(dims, element_type)

        st = tp.sequence_type
        if len(str(st.elem_type)) > 0:
            element_type = self.get_list_element_type(st.elem_type)
            return self.get_list_type(element_type)

        ot = tp.optional_type
        if len(str(ot.elem_type)) > 0:
            element_type = self.get_optional_element_type(ot.elem_type)
            return self.get_optional_type(element_type)

        # Check if TypeProto is empty (sometimes happens for unused function
        # arguments)
        if tp.WhichOneof("value") is None:
            return self.get_none_type()

        # TODO: Others if ever needed. Or we consider ourselves DNN-only.
        # See TypeProto: sequence_type, map_type, optional_type, sparse_tensor_type.
        raise OnnxImportError(f"Unsupported ONNX TypeProto: {tp}")

    def _sanitize_name(self, name):
        if not name.isidentifier():
            name = "_" + name

        # Remove characters that are invalid in MLIR identifier names.
        # https://mlir.llvm.org/docs/LangRef/#identifiers-and-keywords
        return re.sub("[^\w\.]", "_", name)

    def tensor_proto_to_attr(self, tp: onnx.TensorProto) -> Attribute:
        tensor_type = self.tensor_proto_to_builtin_type(tp)
        if tp.HasField("raw_data"):
            # Conveniently, DenseResourceElementsAttr shares the raw data
            # format. We just give it maximum numeric alignment.
            resource = DenseResourceElementsAttr.get_from_buffer(
                tp.raw_data, self._sanitize_name(tp.name), tensor_type, alignment=8
            )
            return resource
        else:
            # We have to do a data type specific instantiation from proto fields.
            # Since this is typically used for small tensor constants, we instantiate
            # as a DenseElementsAttr.
            handler = ELEM_TYPE_INLINE_TENSOR_PROTO_CB.get(tp.data_type)
            if handler is None:
                raise OnnxImportError(f"Unhandled ONNX TensorProto data: {tp}")
            return handler(tp)


def _shallow_copy_and_clear_protobuf_list(protobuf_list) -> list:
    """
    Workaround for .clear() not being available on protobuf lists for some
    reason.
    """
    copy = list(protobuf_list)
    while len(protobuf_list) > 0:
        protobuf_list.pop()
    return copy


def _bind_attributes_on_node(
    interior_node: onnx.NodeProto,
    caller_node: onnx.NodeProto,
    op_schema: onnx.defs.OpSchema,
) -> onnx.NodeProto:
    """
    Helper for _specialize_function_and_create_model() that binds concrete
    values to an attributes on a node in the interior of a function.

    This should behave the same as ONNX's C++ attribute binder, please use it as
    a reference: https://github.com/onnx/onnx/blob/88f8ef15cfaa3138d336f3502aed5018d802bf43/onnx/shape_inference/attribute_binder.h#L15-L64
    """

    def _bind_attributes_in_subgraph(
        old_subgraph: onnx.GraphProto,
        caller_node: onnx.NodeProto,
        op_schema: onnx.defs.OpSchema,
    ) -> onnx.GraphProto:
        """
        Recurse to bind attributes in a subgraph.
        """
        new_subgraph.CopyFrom(old_subgraph)
        old_nodes = _shallow_copy_and_clear_protobuf_list(new_subgraph.node)
        for old_node in old_nodes:
            new_subgraph.node.append(
                _bind_attributes_on_node(old_node, caller_node, op_schema)
            )
        return new_subgraph

    def _bind_attribute(
        old_attribute: onnx.AttributeProto,
        caller_node: onnx.NodeProto,
        op_schema: onnx.defs.OpSchema,
    ) -> Optional[onnx.AttributeProto]:
        """
        Bind a single attribute.

        Bound values either come from attributes on the node calling the
        function, or from default values. If the attribute is optional and has
        no default value, and no value was provided by the caller, None is
        returned and the attribute should be removed.
        """

        ref_name = old_attribute.ref_attr_name
        if not ref_name:
            if not old_attribute.g or len(old_attribute.graphs) == 0:
                return old_attribute

            # Recurse to bind attributes on subgraphs. ONNX's implementation of
            # attribute binding only does this for subgraphs that didn't come
            # from a referenced attribute value, so this code doesn't either.
            new_attribute = onnx.AttributeProto()
            new_attribute.CopyFrom(old_attribute)
            if new_attribute.g:
                new_attribute.g = _bind_attributes_in_subgraph(
                    new_attribute.g, caller_node, op_schema
                )
            if new_attribute.graphs:
                old_subgraphs = _shallow_copy_and_clear_protobuf_list(
                    new_attribute.graphs
                )
                for old_subgraph in old_subgraphs:
                    new_attribute.graphs.append(
                        _bind_attributes_in_subgraph(
                            old_subgraph, caller_node, op_schema
                        )
                    )
            return new_attribute

        for call_attribute in caller_node.attribute:
            if call_attribute.name == ref_name:
                new_attribute = onnx.AttributeProto()
                new_attribute.CopyFrom(call_attribute)
                new_attribute.name = old_attribute.name
                return new_attribute

        # The default value is sometimes empty for optional attributes
        # that don't have a default, in which case it is dropped.
        default_value = op_schema.attributes[ref_name].default_value
        if default_value and default_value.type:
            new_attribute = onnx.AttributeProto()
            new_attribute.CopyFrom(default_value)
            new_attribute.name = old_attribute.name
            return new_attribute

        return None

    new_node = onnx.NodeProto()
    new_node.CopyFrom(interior_node)
    old_attributes = _shallow_copy_and_clear_protobuf_list(new_node.attribute)
    for node_attribute in old_attributes:
        new_attribute = _bind_attribute(node_attribute, caller_node, op_schema)
        if new_attribute is not None:
            new_node.attribute.append(new_attribute)
            continue
    return new_node


def _specialize_function_and_create_model(
    function_proto: onnx.FunctionProto,
    op_schema: onnx.defs.OpSchema,
    name_to_give_model: str,
    input_type_protos: list[onnx.TypeProto],
    output_type_protos: list[onnx.TypeProto],
    caller_node: onnx.NodeProto,
) -> onnx.ModelProto:
    """
    Helper for ModuleCache::get_operator_function() that specializes a function
    and coverts it to a model.

    An ONNX function may be polymorphic, parameterized over the types of its
    inputs and values of its attributes (~= compile-time constants). We need to
    monomorphize it for importing into MLIR. It seems like the only practical
    way to do this is by turning it into a model:
    - models can have types on their inputs and outputs, unlike functions
    - ONNX provides a function to do shape inference (providing concrete
      types for everything in the body) for models, but not for functions
    - the rest of the code in this importer can only handle models, not
      functions
    """

    graph_proto = onnx.GraphProto()

    for input_name, input_type_proto in zip(function_proto.input, input_type_protos):
        input_proto = onnx.ValueInfoProto()
        input_proto.name = input_name
        input_proto.type.CopyFrom(input_type_proto)
        graph_proto.input.append(input_proto)
        output_proto = onnx.ValueInfoProto()

    for output_name, output_type_proto in zip(
        function_proto.output, output_type_protos
    ):
        output_proto.name = output_name
        output_proto.type.CopyFrom(output_type_proto)
        graph_proto.output.append(output_proto)

    for node in function_proto.node:
        # Import referenced attributes from call-site or default values
        graph_proto.node.append(_bind_attributes_on_node(node, caller_node, op_schema))

    graph_proto.name = name_to_give_model

    model_proto = onnx.ModelProto()
    model_proto.opset_import.extend(function_proto.opset_import)
    # FIXME: is this the correct IR version, or should it be the latest, or the
    #        one used by the actual model, or something else?
    model_proto.ir_version = onnx.helper.find_min_ir_version_for(
        function_proto.opset_import
    )
    model_proto.graph.CopyFrom(graph_proto)

    model_proto = onnx.shape_inference.infer_shapes(
        model_proto, check_type=True, strict_mode=True, data_prop=True
    )
    graph_proto = model_proto.graph

    # Useful for debugging.
    # onnx.checker.check_model(model_proto, full_check=True)

    return model_proto


class ModuleCache:
    """Caches per-module lookups of various things."""

    __slots__ = [
        "_m",
        "_cc",
        "_operator_function_map",
    ]

    def __init__(self, module_op: Operation, context_cache: ContextCache):
        self._m = module_op
        self._cc = context_cache
        self._operator_function_map: Dict[str, func_dialect.FuncOp] = {}

    def get_operator_function(
        self,
        op_name: str,
        op_domain: str,
        opset_version: int,
        input_type_protos: list[onnx.TypeProto],
        output_type_protos: list[onnx.TypeProto],
        caller_node: onnx.NodeProto,
        config: Config,
    ) -> Optional[func_dialect.FuncOp]:
        """
        Get or create MLIR function corresponding to an ONNX operator.

        Returns None for ONNX operators that aren't functions.
        """

        allowlists = config.function_expansion_allowlists_by_domain
        denylists = config.function_expansion_denylists_by_domain

        if allowlists is not None and not (
            op_domain in allowlists and op_name in allowlists[op_domain]
        ):
            return None

        if op_domain in denylists and op_name in denylists[op_domain]:
            return None

        op_schema = onnx.defs.get_schema(
            op_name, domain=op_domain, max_inclusive_version=opset_version
        )

        # The get_schema() lookup above should get the right version of the
        # operator definition, but the function body can change slightly
        # within a single operator version, as explained in
        # https://github.com/onnx/onnx/blob/093a8d335a66ea136eb1f16b3a1ce6237ee353ab/onnx/defs/schema.h#L1070-L1086
        # There also seem to be cases where a function goes from being not
        # context-dependent to context-dependent.
        f = lambda ver: ver <= opset_version
        ncd_function_version = max(
            filter(f, op_schema.function_opset_versions),
            default=None,
        )
        cd_function_version = max(
            filter(f, op_schema.context_dependent_function_opset_versions),
            default=None,
        )
        if ncd_function_version is None and cd_function_version is None:
            # No relevant function definition
            return None
        if ncd_function_version is not None and (
            cd_function_version is None or cd_function_version < ncd_function_version
        ):
            specific_version = ncd_function_version
            is_context_dependent = False
        else:
            specific_version = cd_function_version
            is_context_dependent = True

        # This is both a key for memoization of function importing and also a
        # name mangling scheme, so it must include all information needed to
        # uniquely identify a function and anything it might be parameterized
        # over.
        key = repr(
            (
                op_name,
                op_domain,
                opset_version,
                input_type_protos,
                # Though output types can be inferred from input types, it does
                # not seem to be the case that there's only one legal set of
                # outputs for a given set of inputs. When attemtping to always
                # use onnx.shape_inference.infer_function_output_types instead
                # of the caller-provided types, sometimes IR verification fails
                output_type_protos,
                # Avoid including the attributes twice (once on their own and
                # once as part of the node) for context-dependent functions,
                # avoid including unused parts of the node for other functions.
                caller_node if is_context_dependent else caller_node.attribute,
            )
        )

        existing = self._operator_function_map.get(key)
        if existing is not None:
            return existing

        if is_context_dependent:
            function_proto_str = (
                op_schema.get_context_dependent_function_with_opset_version(
                    specific_version,
                    caller_node.SerializeToString(),
                    [
                        t.SerializeToString() if not isinstance(t, bytes) else t
                        for t in input_type_protos
                    ],
                )
            )
        else:
            function_proto_str = op_schema.get_function_with_opset_version(
                specific_version
            )
        if not function_proto_str:
            raise OnnxImportError(
                f"Function lookup for {op_name}/{op_domain}/{specific_version}/{is_context_dependent} failed unexpectedly. This probably indicates a bug."
            )
        function_proto = onnx.onnx_pb.FunctionProto()
        function_proto.ParseFromString(function_proto_str)

        tmp_model_proto = _specialize_function_and_create_model(
            function_proto,
            op_schema,
            key,
            input_type_protos,
            output_type_protos,
            caller_node,
        )

        tmp_model_info = ModelInfo(tmp_model_proto)
        tmp_graph_info = GraphInfo(tmp_model_info, tmp_model_proto.graph)
        # Mark function as private so it will be thrown away after inlining
        imp = NodeImporter.define_function(
            tmp_graph_info, self._m, self._cc, self, private=True
        )
        imp.import_all()
        func_op = imp._p

        self._operator_function_map[key] = func_op
        return func_op


ELEM_TYPE_TO_IR_TYPE_CB = {
    onnx.TensorProto.DataType.FLOAT: lambda: F32Type.get(),
    onnx.TensorProto.DataType.UINT8: lambda: IntegerType.get_unsigned(8),
    onnx.TensorProto.DataType.INT8: lambda: IntegerType.get_signed(8),
    onnx.TensorProto.DataType.UINT16: lambda: IntegerType.get_unsigned(16),
    onnx.TensorProto.DataType.INT16: lambda: IntegerType.get_signed(16),
    onnx.TensorProto.DataType.INT32: lambda: IntegerType.get_signed(32),
    onnx.TensorProto.DataType.INT64: lambda: IntegerType.get_signed(64),
    onnx.TensorProto.DataType.BOOL: lambda: IntegerType.get_signless(1),
    onnx.TensorProto.DataType.FLOAT16: lambda: F16Type.get(),
    onnx.TensorProto.DataType.DOUBLE: lambda: F64Type.get(),
    onnx.TensorProto.DataType.UINT32: lambda: IntegerType.get_unsigned(32),
    onnx.TensorProto.DataType.UINT64: lambda: IntegerType.get_unsigned(64),
    onnx.TensorProto.DataType.COMPLEX64: lambda: ComplexType.get(F32Type.get()),
    onnx.TensorProto.DataType.COMPLEX128: lambda: ComplexType.get(F64Type.get()),
    onnx.TensorProto.DataType.BFLOAT16: lambda: BF16Type.get(),
    onnx.TensorProto.DataType.FLOAT8E4M3FN: lambda: Float8E4M3FNType.get(),
    onnx.TensorProto.DataType.FLOAT8E4M3FNUZ: lambda: Float8E4M3FNUZType.get(),
    onnx.TensorProto.DataType.FLOAT8E5M2: lambda: Float8E5M2Type.get(),
    onnx.TensorProto.DataType.FLOAT8E5M2FNUZ: lambda: Float8E5M2FNUZType.get(),
    onnx.TensorProto.DataType.STRING: lambda: "!torch.str",
    onnx.TensorProto.DataType.UINT4: lambda: IntegerType.get_unsigned(4),
    onnx.TensorProto.DataType.INT4: lambda: IntegerType.get_signed(4),
    # Ommitted: STRING,
}

ELEM_TYPE_SPLAT_TENSOR_PROTO_CB = {
    onnx.TensorProto.DataType.FLOAT: lambda tp, shape: DenseElementsAttr.get_splat(
        RankedTensorType.get(shape, F32Type.get()), FloatAttr.get_f32(tp.float_data[0])
    ),
    onnx.TensorProto.DataType.INT64: lambda tp, shape: DenseElementsAttr.get_splat(
        RankedTensorType.get(shape, IntegerType.get_signed(64)),
        IntegerAttr.get(
            IntegerType.get_signed(64),
            (
                int.from_bytes(tp.raw_data, "little", signed=True)
                if tp.HasField("raw_data")
                else tp.int64_data[0]
            ),
        ),
    ),
    # TODO: All the rest from ELEM_TYPE_TO_IR_TYPE_CB
}

# Mapping of TensorProto.DataType to lambda TensorProto, returning a DenseElementsAttr
# of the builtin tensor type for cases where the tensor data is inlined as typed
# values instead of raw_data.
ELEM_TYPE_INLINE_TENSOR_PROTO_CB = {
    onnx.TensorProto.DataType.FLOAT: lambda tp: DenseElementsAttr.get(
        np.asarray(tp.float_data, dtype=np.float32).reshape(tp.dims), signless=False
    ),
    onnx.TensorProto.DataType.BOOL: lambda tp: DenseElementsAttr.get(
        np.packbits(
            np.asarray(tp.int32_data, dtype=np.bool_).reshape(tp.dims),
            axis=None,
            bitorder="little",
        ),
        shape=tp.dims,
        type=IntegerType.get_signless(1),
    ),
    onnx.TensorProto.DataType.UINT8: lambda tp: DenseElementsAttr.get(
        np.asarray(tp.int32_data, dtype=np.uint8).reshape(tp.dims), signless=False
    ),
    onnx.TensorProto.DataType.INT8: lambda tp: DenseElementsAttr.get(
        np.asarray(tp.int32_data, dtype=np.int8).reshape(tp.dims), signless=False
    ),
    onnx.TensorProto.DataType.INT16: lambda tp: DenseElementsAttr.get(
        np.asarray(tp.int32_data, dtype=np.int16).reshape(tp.dims), signless=False
    ),
    onnx.TensorProto.DataType.INT32: lambda tp: DenseElementsAttr.get(
        np.asarray(tp.int32_data, dtype=np.int32).reshape(tp.dims), signless=False
    ),
    onnx.TensorProto.DataType.INT64: lambda tp: DenseElementsAttr.get(
        np.asarray(tp.int64_data, dtype=np.int64).reshape(tp.dims), signless=False
    ),
    onnx.TensorProto.DataType.DOUBLE: lambda tp: DenseElementsAttr.get(
        np.asarray(tp.double_data, dtype=np.float64).reshape(tp.dims)
    ),
    onnx.TensorProto.DataType.UINT32: lambda tp: DenseElementsAttr.get(
        # Special case. See proto
        np.asarray(tp.uint64_data, dtype=np.uint32).reshape(tp.dims),
        signless=False,
    ),
    onnx.TensorProto.DataType.UINT64: lambda tp: DenseElementsAttr.get(
        np.asarray(tp.uint64_data, dtype=np.uint64).reshape(tp.dims), signless=False
    ),
    # Intentionally unsupported: STRING
}

ELEM_TYPE_TO_NUMPY_DTYPE = {
    onnx.TensorProto.DataType.FLOAT: np.float32,
    onnx.TensorProto.DataType.UINT8: np.uint8,
    onnx.TensorProto.DataType.INT8: np.int8,
    onnx.TensorProto.DataType.UINT16: np.uint16,
    onnx.TensorProto.DataType.INT16: np.int16,
    onnx.TensorProto.DataType.INT32: np.int32,
    onnx.TensorProto.DataType.INT64: np.int64,
    onnx.TensorProto.DataType.BOOL: np.bool_,
    onnx.TensorProto.DataType.FLOAT16: np.float16,
    onnx.TensorProto.DataType.DOUBLE: np.float64,
    onnx.TensorProto.DataType.UINT32: np.uint32,
    onnx.TensorProto.DataType.UINT64: np.uint64,
    onnx.TensorProto.DataType.COMPLEX64: np.complex64,
    onnx.TensorProto.DataType.COMPLEX128: np.complex128,
    # onnx.TensorProto.DataType.BFLOAT16:
    # onnx.TensorProto.DataType.FLOAT8E4M3FN:
    # onnx.TensorProto.DataType.FLOAT8E4M3FNUZ:
    # onnx.TensorProto.DataType.FLOAT8E5M2:
    # onnx.TensorProto.DataType.FLOAT8E5M2FNUZ:
    # Ommitted: STRING,
}

# Mapping of AttributeType code to one of:
#   None: Ignore attribute and do not output to MLIR
#   False: Error if an attribute of this type is present
#   lambda a:AttributeProto, cc: ContextCache that returns an MLIR Attribute
ATTRIBUTE_TYPE_HANDLERS = {
    onnx.AttributeProto.AttributeType.UNDEFINED: False,
    onnx.AttributeProto.AttributeType.FLOAT: lambda a, cc: FloatAttr.get(
        F32Type.get(), a.f
    ),
    onnx.AttributeProto.AttributeType.INT: lambda a, cc: IntegerAttr.get(
        IntegerType.get_signed(64), a.i
    ),
    onnx.AttributeProto.AttributeType.STRING: lambda a, cc: StringAttr.get(a.s),
    onnx.AttributeProto.AttributeType.TENSOR: lambda a, cc: cc.tensor_proto_to_attr(
        a.t
    ),
    onnx.AttributeProto.AttributeType.GRAPH: None,
    onnx.AttributeProto.AttributeType.SPARSE_TENSOR: False,
    onnx.AttributeProto.AttributeType.TYPE_PROTO: False,
    onnx.AttributeProto.AttributeType.FLOATS: lambda a, cc: ArrayAttr.get(
        [FloatAttr.get(F32Type.get(), f) for f in a.floats]
    ),
    onnx.AttributeProto.AttributeType.INTS: lambda a, cc: ArrayAttr.get(
        [IntegerAttr.get(IntegerType.get_signed(64), i) for i in a.ints]
    ),
    onnx.AttributeProto.AttributeType.STRINGS: lambda a, cc: ArrayAttr.get(
        [StringAttr.get(s) for s in a.strings]
    ),
    onnx.AttributeProto.AttributeType.TENSORS: lambda a, cc: ArrayAttr.get(
        [cc.tensor_proto_to_attr(t) for t in a.tensors]
    ),
    onnx.AttributeProto.AttributeType.GRAPHS: False,
    onnx.AttributeProto.AttributeType.SPARSE_TENSORS: False,
    onnx.AttributeProto.AttributeType.TYPE_PROTOS: False,
}


def _get_attr(
    node: onnx.NodeProto, attr_name: str, is_required: bool = True
) -> onnx.AttributeProto:
    for attr in node.attribute:
        if attr.name == attr_name:
            return attr
    if is_required:
        raise OnnxImportError(f"Required attribute {attr_name} not found in {node}")
    return None
