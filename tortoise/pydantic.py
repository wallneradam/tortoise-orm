import functools
import inspect
import re
import typing
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

import tortoise
from tortoise import fields, models

if typing.TYPE_CHECKING:
    from tortoise.models import Model


# Pydantic is an optional dependency
try:
    import pydantic
    from pydantic import BaseModel

    @functools.lru_cache()
    def _get_comments(cls: Type["Model"]) -> Dict[str, str]:
        """
        Get comments exactly before attributes

        It can be multiline comment. The placeholder "{model}" will be replaced with the name of the
        model class.

        :param cls: The class we need to extract comments from its source.
        :return: The dictionary of comments by field name
        """
        source = inspect.getsource(cls)
        comments = {}

        for cls in reversed(cls.__mro__):
            if cls is object:
                continue
            matches = re.findall(rf"((?:(?!\n|^)[^\w\n]*#.*?\n)+?)[^\w\n]*(\w+)\s*[:=]", source)
            for match in matches:
                field_name = match[1]
                # Extract text
                comment = re.sub(r"(^\s*#\s*|\s*$)", "", match[0], flags=re.MULTILINE)
                # Class name template
                comment = comment.replace("{model}", cls.__name__)
                # Change multiline texts to HTML
                comments[field_name] = comment.replace("\n", "<br>")

        return comments

    @functools.lru_cache()
    def _get_annotations(cls: Type["Model"], method: Optional[Callable] = None) -> Dict[str, Any]:
        """
        Get all annotations including base classes
        :param cls: The model class we need annotations from
        :param method: If specified, we try to get the annotations for the callable
        :return: The list of annotations
        """
        globalns = tortoise.Tortoise.apps[cls._meta.app] if cls._meta.app is not None else None
        return typing.get_type_hints(method or cls, globalns=globalns)

    def _get_fetch_fields(
        pydantic_class: Type["PydanticModel"], model_class: Type["Model"]
    ) -> List[str]:
        """
        Recursively collect fields needed to fetch
        :param pydantic_class: The pydantic model class
        :param model_class: The tortoise model class
        :return: The list of fields to be fetched
        """
        fetch_fields = []
        for field_name, field_type in pydantic_class.__annotations__.items():
            origin = getattr(field_type, "__origin__", None)
            if origin is list:
                field_type = field_type.__args__[0]
            # noinspection PyProtectedMember
            if field_name in model_class._meta.fetch_fields and issubclass(
                field_type, PydanticModel
            ):
                subclass_fetch_fields = _get_fetch_fields(
                    field_type, getattr(pydantic_class.__config__, "orig_model")
                )
                if subclass_fetch_fields:
                    fetch_fields.extend([field_name + "__" + f for f in subclass_fetch_fields])
                else:
                    fetch_fields.append(field_name)
        return fetch_fields

    class PydanticModel(BaseModel):
        """ Custom Pydantic BaseModel """

        class Config:
            orm_mode = True  # It should be in ORM mode to convert tortoise data to pydantic

        class Meta:
            # Stores already created pydantic models
            cache: Dict[str, Type["PydanticModel"]] = {}

        # noinspection PyMethodParameters
        @pydantic.validator("*", pre=True, whole=True)  # It is a classmethod!
        def _tortoise_convert(cls, value):  # pylint: disable=E0213
            # Computed fields
            if callable(value):
                return value()
            # Convert ManyToManyRelation to list
            elif isinstance(value, fields.ManyToManyRelation):
                return [v for v in value]
            return value

        @classmethod
        async def from_tortoise(cls, obj: "Model"):
            # Get fields needed to fetch
            fetch_fields = _get_fetch_fields(cls, getattr(cls.__config__, "orig_model"))
            # Fetch fields
            await obj.fetch_related(*fetch_fields)
            # Convert to pydantic object
            values = super().from_orm(obj)
            return values

    def _pydantic_recursion_protector(
        cls: Type["Model"],
        *,
        stack: tuple,
        exclude: Tuple[str] = (),  # type: ignore
        include: Tuple[str] = (),  # type: ignore
        computed: Tuple[str] = (),  # type: ignore
        name=None,
    ) -> Optional[Type[BaseModel]]:
        """
        It is an inner function to protect pydantic model creator against cyclic recursion
        """
        caller_cls, caller_fname, _ = stack[0]
        prop_path = [caller_fname]  # It stores the fields in the hierarchy
        level = 1
        for parent_cls, parent_fname, parent_max_recursion in stack[1:]:
            # Check recursion level
            prop_path.insert(0, parent_fname)
            if level >= parent_max_recursion:
                tortoise.logger.warning(
                    "Recursion level %i has reached for model %s",
                    level,
                    parent_cls.__qualname__ + "." + ".".join(prop_path),
                )
                return None

            if parent_cls is caller_cls and parent_fname == caller_fname:
                tortoise.logger.warning(
                    "Recursion detected: %s", parent_cls.__qualname__ + "." + ".".join(prop_path)
                )
                return None

            level += 1

        return _pydantic_model_creator(
            cls, exclude=exclude, include=include, computed=computed, name=name, stack=stack
        )

    def _pydantic_model_creator(
        cls: Type["Model"],
        *,
        exclude: Tuple[str] = (),  # type: ignore
        include: Tuple[str] = (),  # type: ignore
        computed: Tuple[str] = (),  # type: ignore
        name=None,
        stack: tuple = (),
    ) -> Type[PydanticModel]:
        """
        Inner function to create pydantic model.
        It stores the created models in cache based on arguments
        """
        # Fully qualified class name
        fqname = cls.__module__ + "." + cls.__qualname__

        def get_name() -> str:
            # If arguments are specified (different from the defaults), we append a hash to the
            # class name, to make it unique
            h = (
                ("_" + hex(hash((fqname, exclude, include, computed)))[2:])
                if exclude != () or include != () or computed != ()  # type: ignore
                else ""
            )
            return cls.__name__ + h

        # We need separate model class for different exclude, include and computed parameters
        if not name:
            name = get_name()

        # If we already have this class in cache, use that
        if name in PydanticModel.Meta.cache:
            return PydanticModel.Meta.cache[name]

        # Get settings and defaults
        default_meta = models.Model.Meta
        meta = getattr(cls, "Meta", default_meta)
        default_include = getattr(
            meta, "pydantic_include", getattr(default_meta, "pydantic_include")
        )
        default_exclude = getattr(
            meta, "pydantic_exclude", getattr(default_meta, "pydantic_exclude")
        )
        default_computed = getattr(
            meta, "pydantic_computed", getattr(default_meta, "pydantic_computed")
        )
        backward_relations = getattr(
            meta,
            "pydantic_backward_relations",
            getattr(default_meta, "pydantic_backward_relations"),
        )
        use_comments = getattr(
            meta, "pydantic_use_comments", getattr(default_meta, "pydantic_use_comments")
        )
        max_recursion = getattr(
            meta, "pydantic_max_recursion", getattr(default_meta, "pydantic_max_recursion")
        )
        exclude_raw_fields = getattr(
            meta,
            "pydantic_exclude_raw_fields",
            getattr(default_meta, "pydantic_exclude_raw_fields"),
        )
        sort_fields = getattr(
            meta, "pydantic_sort_fields", getattr(default_meta, "pydantic_sort_fields")
        )

        # Update parameters with defaults
        include = include + default_include
        exclude = exclude + default_exclude
        computed = computed + default_computed

        # Get all annotations
        annotations = _get_annotations(cls)
        # Get field comments
        comments = _get_comments(cls) if use_comments else {}

        # Properties and their annotations` store
        properties: Dict[str, Any] = {"__annotations__": {}}

        # Get model description
        model_description = tortoise.Tortoise.describe_model(cls, serializable=False)

        # Field map we use
        field_map = {}

        def field_map_update(keys: tuple, is_relation=True) -> None:
            for key in keys:
                for fd in model_description[key]:
                    n = fd["name"]
                    # Include or exclude field
                    if (include and n not in include) or n in exclude:  # type: ignore
                        continue
                    # Relations most have annotations to be included if backward_relations
                    # is not True
                    if is_relation and not backward_relations and n not in annotations:
                        continue
                    # Remoce raw fields
                    raw_field = fd.get("raw_field", None)
                    if raw_field is not None and exclude_raw_fields:
                        del field_map[raw_field]
                    field_map[n] = fd

        # Update field definitions from description
        field_map_update(("data_fields",), is_relation=False)
        field_map_update(
            ("fk_fields", "o2o_fields", "m2m_fields", "backward_fk_fields", "backward_o2o_fields")
        )

        # Add possible computed fields
        field_map.update(
            {
                k: {"field_type": callable, "function": getattr(cls, k), "description": None}
                for k in computed
            }
        )

        # Sort field map if requested (Python 3.6 has ordered dictionary keys)
        if sort_fields:
            field_map = {k: field_map[k] for k in sorted(field_map)}

        # Process fields
        for fname, fdesc in field_map.items():
            comment = ""

            field_type = fdesc["field_type"]

            def get_submodel(_model: Type["Model"]) -> PydanticModel:
                """ Get Pydantic model for the submodel """
                nonlocal exclude, name

                new_stack = stack + ((cls, fname, max_recursion),)

                # Get pydantic schema for the submodel
                prefix_len = len(fname) + 1
                pmodel = _pydantic_recursion_protector(
                    _model,
                    exclude=tuple(  # type: ignore
                        [str(v[prefix_len:]) for v in exclude if v.startswith(fname + ".")]
                    ),
                    include=tuple(  # type: ignore
                        [str(v[prefix_len:]) for v in include if v.startswith(fname + ".")]
                    ),
                    computed=tuple(  # type: ignore
                        [str(v[prefix_len:]) for v in computed if v.startswith(fname + ".")]
                    ),
                    stack=new_stack,
                )

                # If the result is None (it is recursion protected) we need to add the field into
                # exclude and get new name for the class to cache different model for it
                if pmodel is None:
                    exclude += (fname,)  # type: ignore
                    name = get_name()

                return pmodel

            # Foreign keys and OneToOne fields are embedded schemas
            if (
                field_type is fields.relational.ForeignKeyFieldInstance
                or field_type is fields.relational.OneToOneFieldInstance
                or field_type is fields.relational.BackwardOneToOneRelation
            ):
                model = get_submodel(fdesc["python_type"])
                if model:
                    properties["__annotations__"][fname] = model

            # Backward FK and ManyToMany fields are list of embedded schemas
            elif (
                field_type is fields.relational.BackwardFKRelation
                or field_type is fields.relational.ManyToManyFieldInstance
            ):
                model = get_submodel(fdesc["python_type"])
                if model:
                    properties["__annotations__"][fname] = List[model]  # type: ignore

            # Computed fields as methods
            elif field_type is callable:
                func = fdesc["function"]
                annotation = _get_annotations(cls, func).get("return", None)
                comment = inspect.cleandoc(func.__doc__).replace("\n", "<br>")  # type: ignore
                if annotation is not None:
                    properties["__annotations__"][fname] = annotation

            # JSON fields are special
            elif field_type is fields.JSONField:
                # We use the annotation if specified, otherwise we default it to dict
                # (The real python_type is not known, it is just a tuple of dict and list)
                annotation = annotations.get(fname, None)
                if annotation is None:
                    tortoise.logger.warning(
                        "JSON fields should be annotated. We use dict as default"
                        " annotation for field '%s'.",
                        fname,
                    )
                properties["__annotations__"][fname] = annotation or dict

            # Any other tortoise fields
            else:
                properties["__annotations__"][fname] = fdesc["python_type"]

            # Create a schema for the field
            if fname in properties["__annotations__"]:
                # Use comment if we have and enabled or use the field description if specified
                description = comment or comments.get(fname, "") or fdesc["description"]
                properties[fname] = pydantic.Field(
                    None, description=description, title=fname.replace("_", " ").title()
                )

        # Because of recursion, it is possible to already have the model in cache, then we need
        # to use that
        try:
            model = PydanticModel.Meta.cache[name]
        except KeyError:
            # Creating Pydantic class for the properties generated before
            model = typing.cast(Type[PydanticModel], type(name, (PydanticModel,), properties))
            # The title of the model to hide the hash postfix
            setattr(model.__config__, "title", cls.__name__)
            # Store the base class
            setattr(model.__config__, "orig_model", cls)
            # Caching model
            PydanticModel.Meta.cache[name] = model

        return model


except ImportError:
    # If pydantic is not installed
    pydantic = None  # type: ignore
    PydanticModel = type  # type: ignore
    _pydantic_model_creator = None  # type: ignore