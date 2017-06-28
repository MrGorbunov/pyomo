#  ___________________________________________________________________________
#
#  Pyomo: Python Optimization Modeling Objects
#  Copyright 2017 National Technology and Engineering Solutions of Sandia, LLC
#  Under the terms of Contract DE-NA0003525 with National Technology and 
#  Engineering Solutions of Sandia, LLC, the U.S. Government retains certain 
#  rights in this software.
#  This software is distributed under the 3-clause BSD License.
#  ___________________________________________________________________________

from six.moves import xrange as range
from six import iteritems, iterkeys

from pyomo.util.plugin import alias
from pyomo.core import *
from pyomo.repn import *
from pyomo.core.base import Transformation
from pyomo.core.base.block import SortComponents
from pyomo.repn import LinearCanonicalRepn
from pyomo.gdp import *

from random import randint

import weakref
import logging
logger = logging.getLogger('pyomo.core')

# DEBUG
from nose.tools import set_trace

class BigM_Transformation(Transformation):

    alias('gdp.bigm', doc="Relaxes a disjunctive model into an algebraic model by adding Big-M terms to all disjunctive constraints.")

    def __init__(self):
        super(BigM_Transformation, self).__init__()
        # QUESTION: The intent was just to make one relaxation block and put
        # all the disjuncts on it like they were on the model, right? So if
        # the name of that block is something I get out of my unique naming
        # function, is this an OK way to keep it for later so that other 
        # disjunctions get put on the same block?
        # Or might this be better if I just store the block? I don't know...
        self.transBlockName = None
        self.handlers = {
            Constraint: self._xform_constraint,
            Var:       False,
            Connector: False,
            Suffix:    False,
            Param:     False,
            Set:       False,
            }


    # QUESTION: I copied and pasted this from add slacks for now, but is there somehwere it can live
    # so that code isn't duplicated?
    def _get_unique_name(self, instance, name):
        # test if this name already exists in model. If not, we're good. 
        # Else, we add random numbers until it doesn't
        while True:
            if not instance.component(name):
                return name
            else:
                name += str(randint(0,9))


    def _apply_to(self, instance, **kwds):
        options = kwds.pop('options', {})

        bigM = options.pop('bigM', None)
        bigM = kwds.pop('bigM', bigM)
        # TODO: this is all changing so that we first use M's from args, then
        # suffixes, then estimate if we still don't have anything.
        if bigM is not None:
            #
            # Test for the suffix - this test will (correctly) generate
            # a warning if the component is already declared, but is a
            # different ctype (e.g., a constraint or block)
            #
            if 'BigM' not in instance.component_map(Suffix):
                instance.BigM = Suffix(direction=Suffix.LOCAL)
            #
            # Note: this will implicitly change the model default BigM
            # value so that the argument overrides the option, which
            # overrides any default specified on the model.
            #
            instance.BigM[None] = bigM

        targets = kwds.pop('targets', None)

        if kwds:
            logger.warning("GDP(BigM): unrecognized keyword arguments:\n%s"
                           % ( '\n'.join(iterkeys(kwds)), ))
        if options:
            logger.warning("GDP(BigM): unrecognized options:\n%s"
                        % ( '\n'.join(iterkeys(options)), ))

        if targets is None:
            for block in instance.block_data_objects(
                    active=True, 
                    sort=SortComponents.deterministic ):
                self._transformBlock(block)
        # ESJ: I've yet to touch this, and I don't get it yet...
        else:
            if isinstance(targets, Component):
                targets = (targets, )
            for _t in target:
                if not _t.active:
                    continue
                if _t.parent_component() is _t:
                    _name = _t.local_name
                    for _idx, _obj in _t.iteritems():
                        if _obj.active:
                            self._transformDisjunction(_name, _idx, _obj)
                else:
                    self._transformDisjunction(
                        _t.parent_component().local_name, _t.index(), _t)


    def _transformBlock(self, block):
        # Transform every (active) disjunction in the block
        for disjunction in block.component_objects(
                Disjunction,
                active=True,
                sort=SortComponents.deterministic):
            self._transformDisjunction(disjunction)

    
    def _transformDisjunction(self, obj): 
        # Put the disjunction constraint on its parent block, then relax
        # each of the disjuncts
        
        # TODO: I think this is redundant? Because this only every gets called
        # from _transformBlock. But if that changed, this is probably still
        # a good idea?
        if not obj.active:
            # Do not transform a block more than once
            return
        
        parent = obj.parent_block()

        # add the XOR (or OR) constraints to parent block (with unique name)
        # It's indexed if this is an IndexedDisjunction.
        orC = Constraint(obj.index_set())
        orC.construct()
        for i in obj.index_set():
            or_expr = 0
            for disjunct in obj[i].disjuncts:
                or_expr += disjunct.indicator_var
            c_expr = or_expr==1 if obj.xor else or_expr >= 1
            orC.add(i, c_expr)
        
        nm = '_xor' if obj.xor else '_or'
        orCname = self._get_unique_name(parent, '_pyomo_gdp_relaxation_' + \
                                            obj.name + nm)
        parent.add_component(orCname, orC)

        # relax each of the disjunctions (or the SimpleDisjunction if it wasn't indexed)
        for i in obj:
            self._transformDisjunctionData(obj[i])

        # deactivate so we know we relaxed
        obj.deactivate()


    def _transformDisjunctionData(self, obj):
        # clone the disjuncts into our new relaxation block (which we'll
        # create if it doesn't exist yet.) We'll relax the disjuncts there.

        # TODO: Emma, this is bad... relaxation block isn't necessarily on this guy's
        # parent component. Need to look for it on the model and create it there if it
        # isn't there already.
        parent = obj.parent_block()
        # make sure that we have a relaxation block.
        if self.transBlockName is None:
            self.transBlockName = self._get_unique_name(parent, '_pyomo_gdp_relaxation')
            parent.add_component(self.transBlockName, Block())
        transBlock = parent.component(self.transBlockName)

        # build block structure on transformation block to mirror disjunct hierarchy
        for disjunct in obj.disjuncts:
            disj_parent = disjunct.parent_component()
            clonedDisj = transBlock.component(disj_parent.name)
            if clonedDisj is None:
                clonedDisj = Block(disj_parent.index_set())
                transBlock.add_component(disj_parent.name, clonedDisj)

            # ESJ: TODO: Right now I am just going to pass the transformation block
            # through so that I have it.
            self._bigM_relax_disjunct(disjunct, transBlock)

        # deactivate the disjunction so we know we've relaxed it
        obj.deactivate()


    def _bigM_relax_disjunct(self, disjunct, transBlock):
        if not disjunct.active:
            disjunct.indicator_var.fix(0)
            return
        # ESJ: TODO: this is going to be something else... Maybe whether or not the 
        # constraint is copied over? But no... Because of the belonging to multiple
        # disjunctions issue. It still should only ge relaxed once. Oh, so it is whether
        # or not the constraints on the original disjunction is deactivated yet or no?
        if disjunct.parent_block().local_name.startswith('_gdp_relax'):
            # Do not transform a block more than once
            return

        # _tmp = disjunct.parent_block().component('_gdp_relax')
        # if _tmp is None:
        #     _tmp = Block()
        #     disjunct.parent_block().add_component('_gdp_relax', _tmp)

        # Move this disjunct over to a Block component (so the writers
        # will pick it up)
        # if disjunct.parent_component().dim() == 0:
        #     # Since there can't be more than one Disjunct in a
        #     # SimpleDisjunct, then we can just reclassify the entire
        #     # component into our scratch space
        #     disjunct.parent_block().del_component(disjunct)
        #     _tmp.add_component(disjunct.local_name, disjunct)
        #     _tmp.reclassify_component_type(disjunct, Block)
        # else:
        #     _block = _tmp.component(disjunct.parent_component().local_name)
        #     if _block is None:
        #         _block = Block(disjunct.parent_component().index_set())
        #         _tmp.add_component(disjunct.parent_component().local_name, _block)
        #     # Move this disjunction over to the Constraint
        #     idx = disjunct.index()
        #     _block._data[idx] = disjunct.parent_component()._data.pop(idx)
        #     _block._data[idx]._component = weakref.ref(_block)

        # Transform each component within this disjunct
        for name, obj in list(disjunct.component_map().iteritems()):
            handler = self.handlers.get(obj.type(), None)
            if not handler:
                if handler is None:
                    raise GDP_Error(
                        "No BigM transformation handler registered "
                        "for modeling components of type %s" % obj.type() )
                continue
            handler(name, obj, disjunct, transBlock)
        # deactivate disjunct so we know we've relaxed it
        disjunct.deactivate()


    def _xform_constraint(self, _name, constraint, disjunct, transBlock):
        # add constraint to the transformation block, we'll transform it there.
        mirrorDisj = transBlock.component(disjunct.parent_component().name)[disjunct.index()]
        #mirrorDisj.add_component(constraint.name, Constraint(expr=constraint.expr.clone()))
        #constraint.deactivate()

        if 'BigM' in disjunct.component_map(Suffix):
            M = disjunct.component('BigM').get(constraint)
        else:
            M = None
        lin_body_map = getattr(disjunct.model(),"lin_body",None)
        for cname, c in iteritems(constraint._data):
            if not c.active:
                continue
            c.deactivate()

            name = _name + ('.'+str(cname) if cname is not None else '')

            if (not lin_body_map is None) and (not lin_body_map.get(c) is None):
                raise GDP_Error('GDP(BigM) cannot process linear ' \
                      'constraint bodies (yet) (found at ' + name + ').')

            if isinstance(M, list):
                if len(M):
                    m = M.pop(0)
                else:
                    m = (None,None)
            else:
                m = M
            if not isinstance(m, tuple):
                if m is None:
                    m = (None, None)
                else:
                    m = (-1*m,m)
            
            # If we need an M (either for upper and/or lower bounding of
            # the expression, then try and estimate it
            if ( c.lower is not None and m[0] is None ) or \
                   ( c.upper is not None and m[1] is None ):
                m = self._estimate_M(c.body, name, m, disjunct)

            bounds = (c.lower, c.upper)
            for i in (0,1):
                if bounds[i] is None:
                    continue
                if m[i] is None:
                    raise GDP_Error("Cannot relax disjunctive " + \
                          "constraint %s because M is not defined." % name)
                n = name;
                if bounds[1-i] is None:
                    n += '_eq'
                else:
                    n += ('_lo','_hi')[i]

                if __debug__ and logger.isEnabledFor(logging.DEBUG):
                    logger.debug("GDP(BigM): Promoting local constraint "
                                 "'%s' as '%s'", constraint.local_name, n)
                # TODO: this will be true when we can leave the indicator
                # vars in the disjunct:
                M_expr = (m[i]-bounds[i])*(1-disjunct.indicator_var)
                # But for now:
                #M_expr = (m[i] - bounds[i])*(1-mirrorDisj.indicator_var)
                if i == 0:
                    newC = Constraint(expr=c.lower <= c.body - M_expr)
                else:
                    newC = Constraint(expr=c.body - M_expr <= c.upper)
                mirrorDisj.add_component(n, newC)
                #disjunct.add_component(n, newC)
                newC.construct()


    def _estimate_M(self, expr, name, m, disjunct):
        # Calculate a best guess at M
        repn = generate_canonical_repn(expr)
        M = [0,0]

        if isinstance(repn, LinearCanonicalRepn):
            if repn.constant != None:
                for i in (0,1):
                    if M[i] is not None:
                        M[i] += repn.constant

            for i, coef in enumerate(repn.linear or []):
                var = repn.variables[i]
                coef = repn.linear[i]
                bounds = (value(var.lb), value(var.ub))
                for i in (0,1):
                    # reverse the bounds if the coefficient is negative
                    if coef > 0:
                        j = i
                    else:
                        j = 1-i

                    try:
                        M[j] += value(bounds[i]) * coef
                    except:
                        M[j] = None
        else:
            logger.info("GDP(BigM): cannot estimate M for nonlinear "
                        "expressions.\n\t(found while processing %s)",
                        name)
            M = [None,None]


        # Allow user-defined M values to override the estimates
        for i in (0,1):
            if m[i] is not None:
                M[i] = m[i]

        # Search for global BigM values: if there are still undefined
        # M's, then search up the block hierarchy for the first block
        # that contains a BigM Suffix with a non-None value for the
        # "None" component.
        if None in M:
            m = None
            while m is None and disjunct is not None:
                if 'BigM' in disjunct.component_map(Suffix):
                    m = disjunct.component('BigM').get(None)
                disjunct = disjunct.parent_block()
            if m is not None:
                try:
                    # We always allow M values to be specified as pairs
                    # (for lower / upper bounding)
                    M = [m[i] if x is None else x for i,x in enumerate(M)]
                except:
                    # We assume the default M is positive (so we need to
                    # invert it for the lower-bound M)
                    M = [(2*i-1)*m if x is None else x for i,x in enumerate(M)]

        return tuple(M)

